// Fixed, headless entry point for Windows PowerShell inside the per-task LPAC.
// Build with the Windows .NET Framework compiler and the OS PowerShell SDK.
using System;
using System.IO;
using System.Management.Automation;
using System.Management.Automation.Runspaces;
using System.Runtime.InteropServices;
using System.Text;

internal static class FleetPowerShell
{
    [DllImport("ole32.dll")]
    private static extern int CoInitializeEx(IntPtr reserved, uint apartment);

    [DllImport("ole32.dll")]
    private static extern void CoUninitialize();

    private static void SetupOutput()
    {
        var encoding = new UTF8Encoding(false);
        Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), encoding) { AutoFlush = true });
        Console.SetError(new StreamWriter(Console.OpenStandardError(), encoding) { AutoFlush = true });
    }

    [MTAThread]
    private static int Main(string[] args)
    {
        SetupOutput();
        if (args.Length == 0)
            return 2;

        // The stock authorization manager uses URLMON/COM to check the local
        // script's zone and execution policy. Initialize it on the runspace thread.
        int initialized = CoInitializeEx(IntPtr.Zero, 0);
        try
        {
            Marshal.ThrowExceptionForHR(initialized);
            using (var runspace = RunspaceFactory.CreateRunspace())
            using (var pipeline = PowerShell.Create())
            using (var output = new PSDataCollection<PSObject>())
            {
                runspace.ThreadOptions = PSThreadOptions.UseCurrentThread;
                runspace.Open();
                // Use a drive rooted at the authorized snapshot. The security token
                // cannot traverse or enumerate the host volume's private ancestors.
                using (var setup = PowerShell.Create())
                {
                    setup.Runspace = runspace;
                    setup.AddCommand("New-PSDrive").AddParameter("Name", "FleetWork")
                        .AddParameter("PSProvider", "FileSystem").AddParameter("Scope", "Global")
                        .AddParameter("Root", Directory.GetCurrentDirectory()).AddParameter("ErrorAction", "Stop");
                    setup.Invoke();
                }
                runspace.SessionStateProxy.Path.SetLocation(@"FleetWork:\");
                pipeline.Runspace = runspace;
                pipeline.AddCommand(args[0]);
                for (int i = 1; i < args.Length; i++)
                    pipeline.AddArgument(args[i]);

                output.DataAdded += (s, e) => Console.WriteLine(output[e.Index]);
                pipeline.Streams.Error.DataAdded += (s, e) => Console.Error.WriteLine(pipeline.Streams.Error[e.Index]);
                pipeline.Streams.Warning.DataAdded += (s, e) => Console.Error.WriteLine(pipeline.Streams.Warning[e.Index]);
                pipeline.Streams.Information.DataAdded += (s, e) => Console.WriteLine(pipeline.Streams.Information[e.Index].MessageData);
                pipeline.Invoke<PSObject>(null, output);
                object exit = runspace.SessionStateProxy.GetVariable("LASTEXITCODE");
                if (exit is int)
                    return (int)exit;
                var state = pipeline.InvocationStateInfo.State;
                // A handled error can still set HadErrors. Preserve normal
                // -File completion when the script catches it and continues.
                return state == PSInvocationState.Failed || state == PSInvocationState.Stopped ? 1 : 0;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
        finally
        {
            if (initialized >= 0)
                CoUninitialize();
        }
    }
}
