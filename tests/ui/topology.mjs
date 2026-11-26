import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";

// Use two phases of the browser's own dash animation to measure signed travel.
// Pausing for the samples makes the comparison independent of browser frame rate.
async function direction(branch, selector) {
  return branch.locator(".topo-arm .topo-lane.topo-" + selector).evaluate((lane) => {
    const style = () => getComputedStyle(lane, "::before");
    const anim = lane.getAnimations({ subtree: true }).find((a) => a.animationName === style().animationName);
    if (!anim || anim.playState === "paused") return 0;
    if (!style().backgroundImage.includes("gradient") || style().backgroundRepeat !== "repeat-x") throw Error("Expected a visible dashed track");
    const original = anim.currentTime;
    const duration = anim.effect.getComputedTiming().duration;
    anim.pause();
    function offset(phase) {
      anim.currentTime = duration * phase;
      const value = style().backgroundPositionX;
      return Number(value.match(/(-?[\d.]+)px/)?.[1] ?? parseFloat(value));
    }
    try {
      const start = offset(.2);
      return Math.sign(offset(.6) - start);
    } finally {
      anim.currentTime = original;
      anim.play();
    }
  });
}

export async function checkTopologyEnrollment(page, topology, hub, token, snap) {
  // A disposable fourth node on loopback, using the same pairing API as a new Sidecar.
  let secret;
  let reachable = false;
  const peer = createServer((req, res) => {
    const valid = req.url === "/health" && reachable && secret && req.headers.authorization === "Bearer " + secret;
    res.writeHead(valid ? 200 : 503, { "Content-Type": "application/json" }).end(valid ? '{"active_tasks":0}' : "{}");
  });
  peer.listen(0, "127.0.0.1");
  await once(peer, "listening");
  let id;
  const name = "Maple";
  const endpoint = page.getByRole("button", { name: "查看节点 " + name, exact: true });
  const branch = topology.locator(".topo-branch").filter({ has: endpoint });
  const branches = topology.locator(".topo-node");
  const before = await branches.count();
  const timeout = { timeout: 25_000 }; // Includes the Hub probe and Fleet's inventory refresh.
  try {
    const enroll = await page.request.post(hub + "/api/enroll", { data: {
      token, alias: name, url: "http://127.0.0.1:" + peer.address().port,
    } });
    assert.equal(enroll.status(), 200);
    ({ id, secret } = await enroll.json());
    await endpoint.getByText("离线", { exact: true }).waitFor(timeout);
    assert.equal(await branches.count(), before + 1);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    assert.equal(await direction(branch, "out"), 0);
    assert.equal(await direction(branch, "back"), 0);
    reachable = true;
    await endpoint.getByText("在线", { exact: true }).waitFor(timeout);
    await endpoint.click();
    await page.getByRole("dialog").getByRole("button",{name:"关闭弹窗",exact:true}).click();
    for (const [width, height, screenshot] of [[390, 844, "24-topology-joined"], [1440, 900, "25-topology-joined-desktop"]]) {
      await page.setViewportSize({ width, height });
      const geometry = await branch.evaluate((b) => {
        const card = b.querySelector(".topo-node").getBoundingClientRect();
        const lanes = b.querySelector(".topo-arm").getBoundingClientRect();
        return { gap: card.left - lanes.right, centered: Math.abs((card.top + card.bottom - lanes.top - lanes.bottom) / 2) };
      });
      assert.ok(Math.abs(geometry.gap) <= 2 && geometry.centered <= 6, "New branch connects to its node at " + width);
      assert.equal(await direction(branch, "out"), 1);
      assert.equal(await direction(branch, "back"), -1);
      await topology.scrollIntoViewIfNeeded();
      await snap(screenshot);
    }
    for (const [label, outgoing, incoming] of [["下发", 1, 0], ["回传", 0, -1], ["全景", 1, -1]]) {
      await topology.getByRole("button", { name: label, exact: true }).click();
      assert.equal(await direction(branch, "out"), outgoing);
      assert.equal(await direction(branch, "back"), incoming);
    }
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await direction(branch, "out"), 0);
    assert.equal(await direction(branch, "back"), 0);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const disabled = await page.request.patch(hub + "/api/nodes/" + id, { data: { enabled: false } });
    assert.equal(disabled.status(), 200);
    await endpoint.getByText("已停用", { exact: true }).waitFor(timeout);
    assert.equal(await direction(branch, "out"), 0);
    assert.equal(await direction(branch, "back"), 0);
  } finally {
    try {
      if (id) assert.equal((await page.request.delete(hub + "/api/nodes/" + id)).status(), 200);
    } finally {
      peer.closeAllConnections();
      await new Promise((resolve) => peer.close(resolve));
    }
  }
  await endpoint.waitFor({ ...timeout, state: "detached" });
  assert.equal(await branches.count(), before);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
}
