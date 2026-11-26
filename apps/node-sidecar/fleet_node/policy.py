import asyncio
import json
import re
import time
import uuid

from packages.protocol.adapters import Decision

DEFAULT_RULES = [
    r"(?i)\b(rm|remove-item|rd|rmdir)\b[^\n]*(?:-[rRfF]|-recurse|-force|/s\b)",
    r"(?i)\b(format(?:-volume)?|mkfs(?:\.\w+)?|diskpart|reg(?:\.exe)?|systemctl|sc\.exe|netsh|iptables|ufw|set-service|set-netfirewallrule)\b",
    r"(?i)\b(?:git\s+push|curl|wget|invoke-webrequest|invoke-restmethod|scp|rsync)\b",
    r"(?i)\b(?:sudo|runas|pkexec|elevate|setcap|diskutil\s+erase\w*|clear-disk|initialize-disk)\b",
    r"(?i)\b(?:new|set|remove|start|stop|restart)-(?:netfirewall\w*|service)\b",
    r"(?i)\b(?:HKLM|HKCU|HKCR|HKU|HKCC|HKEY_(?:LOCAL_MACHINE|CURRENT_USER|CLASSES_ROOT))[:\\]",
    r"(?i)(?:\.ssh|\.aws|\.credentials|\.env\b|private.key|access.token|api.key)",
]


def compile_rules(values):
    if (
        not isinstance(values, list)
        or len(values) > 48
        or any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in values
        )
    ):
        raise ValueError("Rules must be a list of at most 48 regular expressions")
    return [re.compile(value) for value in values]


def within(path, roots):
    return any(path.is_relative_to(root) for root in roots)


class TaskPolicy:
    def __init__(self, node, workspace, emit, adapter):
        self.node, self.workspace = node, workspace
        self.emit, self.adapter = emit, adapter
        self.session_dirs = set()
        self.pending = {}

    async def authorize(self, command, paths, high_risk=False):
        targets = [(self.workspace / path).resolve() for path in paths] or [
            self.workspace
        ]
        outside = [
            target
            for target in targets
            if not within(target, self.node.roots | self.session_dirs)
        ]
        if outside and not self.node.arbitrary:
            raise PermissionError("Target is outside trusted workspaces")
        high_risk = high_risk or any(rule.search(command) for rule in self.node.rules)
        if not high_risk and not outside:
            return True
        identifier = uuid.uuid4().hex
        reason = "命中高危规则" if high_risk else "白名单外目录"
        trust_dir = outside[0] if outside and outside[0].is_dir() else None
        if outside and trust_dir is None:
            trust_dir = outside[0].parent
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        self.emit(
            "approval_required",
            {
                "id": identifier,
                "reason": reason,
                "workspace": str(self.workspace),
                "command": command,
                "paths": list(map(str, targets)),
                "expires_at": time.time() + self.node.timeout,
                "trust_dir": str(trust_dir) if trust_dir else None,
            },
        )
        decision = Decision.deny
        cause = "cancelled"
        try:
            async with asyncio.timeout(self.node.timeout):
                decision = await future
            cause = "operator"
            if decision == Decision.directory and trust_dir is not None:
                self.session_dirs.add(trust_dir)
            return decision != Decision.deny
        except TimeoutError:
            cause = "timeout"
            return False
        finally:
            self.pending.pop(identifier, None)
            self.emit(
                "approval_resolved",
                {"id": identifier, "decision": decision.value, "cause": cause},
            )

    def decide(self, identifier, decision):
        future = self.pending.get(identifier)
        if future is None or future.done():
            return False
        self.adapter.approval_response(future, decision)
        return True


def describe_tool(name, arguments):
    return f"{name} {json.dumps(arguments, ensure_ascii=False)}"
