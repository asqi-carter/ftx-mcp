using System;
using System.Globalization;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using UAManagedCore;
using OpcUa = UAManagedCore.OpcUa;
using FTOptix.HMIProject;
using FTOptix.NetLogic;
using FTOptix.CoreBase;
// Studio injects `using FTOptix.RAEtherNetIP;` into every NetLogic when an
// EtherNet/IP driver is added, and that namespace has its own Encoding type.
// Alias so all four System.Text.Encoding call sites stay unambiguous.
using Encoding = System.Text.Encoding;
using FTOptix.WebUI;

// StudioMCPBridge - a design-time NetLogic that hosts a loopback HTTP bridge into
// the live Optix model (read + author) for the ftx-mcp service.
//
// How it runs: [ExportMethod] StartBridge (right-click) spawns a background
// TcpListener on loopback (avoids the http.sys URL-ACL/admin wall for a
// non-elevated Studio); Project.Current resolves at design time. StopBridge signals
// the loop via a NAMED KERNEL EVENT, because Studio isolates each ExportMethod in
// its own AssemblyLoadContext - so shared managed state never reaches the listener
// (see StopListener / Loop).
//
// The NetLogic CLASS NAME must equal its node name in Optix. Shipped as the
// "MCPBridge" Optix library (component "StudioMCPBridge") for drag-in reuse.
public class StudioMCPBridge : BaseNetLogic
{
    // kept in sync with the ftx-mcp service version (pyproject.toml /
    // service/__init__.py / server.json) rather than left to lag behind it. Bump
    // this alongside those whenever a change to THIS FILE actually alters bridge
    // behavior; leave it alone for service/dashboard-only releases that never
    // touch StudioMCPBridge.cs, since those genuinely don't change what the
    // bridge does (a mismatch there is cosmetic - the string is just stale, not
    // wrong about behavior). Last bumped for the 1.0.8 batch: the I31 crash-proof
    // dispatch + enum coercion, the NetLogic move refusal, I35's dotnet_type,
    // relative dynamic links inside an ObjectType, and OpenKeyboard - all real
    // behavior changes to this file.
    private const string BridgeVersion = "1.0.8";
    // multi-instance support (v1.0.7). Port is no longer a single
    // fixed const - each Studio instance self-assigns the first free port in
    // BasePort..BasePort+PortRangeSize-1, so up to PortRangeSize projects can
    // run this NetLogic (StartBridge'd) AT THE SAME TIME, each on its own port,
    // instead of exclusively fighting over :8768. The Python service discovers
    // which project lives on which port by probing the whole range (see
    // service/core.py's bridge registry). PortRangeSize=4 covers 4 simultaneous
    // Studio instances; raise it if you routinely run more.
    private const int BasePort = 8768;
    private const int PortRangeSize = 4;   // ports 8768..8771

    // Cross-ALC stop signal: at design time Studio runs each [ExportMethod] in an
    // ISOLATED AssemblyLoadContext, so StartBridge and StopBridge share NO managed
    // state (neither instance NOR static - both were tried and failed).
    // A named kernel event IS shared across ALCs in the process; StopBridge sets it and
    // the accept loop (in whichever ALC owns the listener) polls it and closes the socket.
    //
    // the event name USED TO be a single compile-time constant
    // (".._p8768"), which worked precisely because it needed no runtime state -
    // every ALC agreed on the same literal string. Now that the port is chosen
    // at runtime (per instance), the name has to be too - but StopBridge runs in
    // a FRESH ALC that never ran StartListener, so it cannot read a static field
    // to learn which port THIS Studio process bound. Environment.SetEnvironmentVariable
    // (default target = Process) is the one piece of state that DOES survive an
    // ALC reload within the same process without being visible to *other*
    // Studio.exe processes - so it's what carries the bound port from
    // StartListener to a later StopBridge, keeping StopBridge scoped to only
    // this instance's own bridge, never a sibling Studio's.
    //
    // this USED TO hold a single port number (whatever
    // StartListener bound most recently), which meant StopBridge could only ever
    // recover ONE listener. That broke down for a very ordinary sequence: edit
    // this NetLogic's code (or just fire StartBridge again quickly after a
    // StopBridge, before the old listener thread noticed the stop signal and
    // actually closed its socket - see Loop's ~50ms poll) - the new StartBridge
    // lands on a DIFFERENT port and overwrites this value, and the OLD port
    // becomes permanently un-signallable: nothing remembers it exists anymore,
    // even though the underlying per-port named stop-event (OpenStopEvent) would
    // happily have stopped it if only something had asked. Confirmed live: an
    // edited+rebuilt copy of this NetLogic left the PREVIOUS compiled listener
    // thread serving the same project on its old port indefinitely, invisible to
    // StopBridge, fixable only by closing Studio outright.
    //
    // Now holds a COMMA-SEPARATED LIST of every port this Studio PROCESS has
    // bound and not yet confirmed torn down - appended to by StartListener,
    // with each port removing ONLY ITSELF (never the whole list) once its own
    // Loop() thread actually exits (see Loop's finally). StopBridge sweeps and
    // signals every port in the list, not just one - see ReadBoundPorts /
    // AddBoundPort / RemoveBoundPort below, and StopListener.
    private const string BoundPortEnvVar = "FTX_STUDIOBRIDGE_BOUND_PORT";
    // Named kernel mutex guarding the list's read-modify-write cycle (parse,
    // mutate, re-serialize) against concurrent AddBoundPort/RemoveBoundPort
    // calls from OTHER ALCs' threads - a plain C# lock only protects callers
    // within the SAME ALC (statics, including lock objects, don't cross ALC
    // boundaries; see the class-level comment on _listener), so this needs the
    // same named-kernel-object trick OpenStopEvent already relies on.
    private const string PortListMutexName = "Local\\StudioMCPBridge_PortList_Mutex";

    private static string StopEventNameFor(int port) => "Local\\StudioMCPBridge_Stop_p" + port;

    private const int MaxItems = 500;
    // unlike the bridge's own TCP port (self-assigned per instance,
    // see BasePort/PortRangeSize above), the Web presentation engine port is a
    // PROJECT setting, persisted into the project's own model by SetupProject -
    // it does NOT auto-negotiate at bridge-start time. Every project SetupProject
    // has ever been run against defaults to this SAME port, so running more than
    // one project's EMULATOR at once (not just bridging them) needs each project
    // reconfigured to a distinct port first, e.g. by pairing it with the bridge
    // port: 8768<->8081 (this default), 8769<->8082, 8770<->8083, 8771<->8084.
    // Pass an explicit port to SetupProject/EnsureWebEngineCore (or POST
    // /bridge/setup/web-engine?port=N) to assign one of those to a project.
    private const int WebEnginePort = 8081;   // default Web presentation engine port
    private const int WebEngineMaxConnections = 5;   // Studio's default; 0/absent = no serve
    // Studio's default AllowedLocalSources allow-list; a fresh MakeObject leaves it
    // empty, and the runtime then blocks images/fonts/css/js from serving.
    private static readonly string[] WebEngineAllowedSources = {
        "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.svg", "*.avi", "*.mov",
        "*.mkv", "*.mpg", "*.mp4", "*.wmv", "*.pdf", "*.ttf", "*.otf", "*.html",
        "*.css", "*.js", "*.mjs" };

    // Static so the loop and same-ALC callers share one listener reference. NOTE: this
    // does NOT fix cross-ALC StopBridge (instance AND static were tried, 0.9.3/0.9.4,
    // and neither crosses the ALC) - the named event above is what does. Cross-assembly
    // reload orphans still require the Studio closed.
    private static TcpListener _listener;
    private static volatile bool _running;
    // the port THIS running listener bound to (or -1 if none is
    // running). Valid within the ALC/thread epoch that started it; StopBridge
    // (a fresh ALC) uses BoundPortEnvVar instead, not this field, to learn it.
    private static volatile int _boundPort = -1;

    // Opportunistic probe: design-time Start() is NOT expected to auto-fire.
    public override void Start()
    {
        StartListener("Start()");
    }

    public override void Stop()
    {
        StopListener();
    }

    // PRIMARY entry. Right-click this NetLogic node in Studio -> StartBridge.
    [ExportMethod]
    public void StartBridge()
    {
        StartListener("StartBridge()");
    }

    [ExportMethod]
    public void StopBridge()
    {
        StopListener();
    }

    // Right-click this node -> Execute CheckFormula to syntax-check an
    // ExpressionEvaluator formula BEFORE wiring it (Optix only validates at runtime,
    // where a bad formula silently no-ops). Result goes to the Studio Output. The
    // bridge's attach-expression endpoint + POST /bridge/expr/validate run the SAME
    // ValidateExpressionSyntax check, so the operator and the model client agree.
    // DELIBERATELY NOT named "ValidateExpression": that name collided with FTOptix's
    // own expression subsystem, which invoked this ExportMethod with a mismatched arg
    // count (TargetParameterCountException, observed live 2026-07-26 while editing
    // converter expressions), breaking design-time expression validation. Do not rename
    // back to ValidateExpression.
    [ExportMethod]
    public void CheckFormula(string expression, string sources)
    {
        int n = CountSources(sources);
        var err = ValidateExpressionSyntax(expression, n);
        if (err == null)
            Log.Info("StudioBridge", "CheckFormula OK (" + n + " source(s)): " + expression);
        else
            Log.Error("StudioBridge", "CheckFormula INVALID: " + err + "  [" + expression + "]");
    }

    // Visible setup action: right-click this NetLogic node in Studio -> SetupProject.
    // Ensures a Web presentation engine exists (Port/Protocol/StartWindow/StyleSheet/
    // MaxNumberOfConnections) so the deployed runtime can serve a browser canvas. Runs
    // directly against Project.Current at design time - does NOT need the bridge started.
    [ExportMethod]
    public void SetupProject()
    {
        Log.Info("StudioBridge", "SetupProject: " + EnsureWebEngineCore(WebEnginePort, "0.0.0.0"));
        // this instance's bridge landed on a non-default port (i.e.
        // :8768 was already taken by another project's bridge), which is a
        // reasonable signal you're running more than one project at once - flag
        // that the just-assigned web-engine port (8081) will collide with any
        // OTHER project also left at 8081 if their emulators run simultaneously,
        // and name this project's paired suggestion (see the WebEnginePort
        // comment above for the full 8768<->8081.. table).
        if (_boundPort > BasePort)
        {
            int suggested = WebEnginePort + (_boundPort - BasePort);
            Log.Warning("StudioBridge", "SetupProject: this project's bridge is on port " +
                _boundPort + " (not the default " + BasePort + "), suggesting more than one " +
                "project is in play - the web-engine port just set is still the default " +
                WebEnginePort + ", which will COLLIDE with any other project also left at " +
                WebEnginePort + " if both emulators run at the same time. Re-run with an " +
                "explicit port (POST /bridge/setup/web-engine?port=" + suggested +
                ") to give this project its own.");
        }
    }

    // Create-or-open the process-global stop event (ManualReset) for a specific
    // port. Shared across ALCs by name, so a StopBridge in one ALC can signal a
    // listener loop in another - as long as both agree on the port, which is why
    // StartListener/StopListener resolve it through BoundPortEnvVar rather than a
    // static field (see the comment on BoundPortEnvVar above).
    private static EventWaitHandle OpenStopEvent(int port)
    {
        return new EventWaitHandle(false, EventResetMode.ManualReset, StopEventNameFor(port));
    }

    // BoundPortEnvVar port-list helpers. See the
    // comment on BoundPortEnvVar's declaration for why this exists (a single
    // remembered port silently orphaned any PREVIOUS listener once a second
    // StartBridge landed on a different port).
    //
    // ReadBoundPorts is an unlocked snapshot read - fine for StopListener's
    // sweep, which just needs "recent enough" (worst case it misses a port
    // added a moment ago and cleans it up on the next Stop). AddBoundPort /
    // RemoveBoundPort do a locked read-modify-write via PortListMutexName so
    // two concurrent mutations (e.g. a new StartListener adding its port at
    // the same moment an old Loop() thread is removing its own) can't clobber
    // each other.
    private static List<int> ParsePortList(string raw)
    {
        var result = new List<int>();
        if (string.IsNullOrEmpty(raw)) return result;
        foreach (var tok in raw.Split(','))
        {
            int p;
            if (int.TryParse(tok.Trim(), out p) && !result.Contains(p)) result.Add(p);
        }
        return result;
    }

    private static List<int> ReadBoundPorts()
    {
        return ParsePortList(Environment.GetEnvironmentVariable(BoundPortEnvVar));
    }

    private static void MutateBoundPorts(Action<List<int>> mutate)
    {
        using (var mtx = new Mutex(false, PortListMutexName))
        {
            bool owned = false;
            try
            {
                // Bounded wait, not indefinite - a stuck/abandoned mutex must never
                // hang a StartBridge/StopBridge click forever.
                owned = mtx.WaitOne(TimeSpan.FromSeconds(2));
                var ports = ParsePortList(Environment.GetEnvironmentVariable(BoundPortEnvVar));
                mutate(ports);
                Environment.SetEnvironmentVariable(BoundPortEnvVar, ports.Count == 0 ? null : string.Join(",", ports));
            }
            catch (AbandonedMutexException)
            {
                // The mutex's previous owner exited (e.g. Studio crashed) mid-update -
                // we still got ownership; the list may be slightly stale but that's
                // self-healing (next Add/Remove or StopBridge sweep corrects it).
                var ports = ParsePortList(Environment.GetEnvironmentVariable(BoundPortEnvVar));
                mutate(ports);
                Environment.SetEnvironmentVariable(BoundPortEnvVar, ports.Count == 0 ? null : string.Join(",", ports));
                owned = true;
            }
            finally
            {
                if (owned) { try { mtx.ReleaseMutex(); } catch { /* ignore */ } }
            }
        }
    }

    private static void AddBoundPort(int port)
    {
        MutateBoundPorts(ports => { if (!ports.Contains(port)) ports.Add(port); });
    }

    private static void RemoveBoundPort(int port)
    {
        MutateBoundPorts(ports => ports.Remove(port));
    }

    // ---- registry file helpers ---------------------------------------------
    // Each armed bridge writes %LOCALAPPDATA%\ftx-mcp\bridges\<port>.json on
    // successful bind and deletes it on stop / listener exit. The Python
    // service reads the directory first (one /bridge/health confirm per file)
    // instead of probing the whole port range, making multi-instance discovery
    // faster and adding pid + project_path that the range scan cannot provide.

    private static string RegistryDir()
    {
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return System.IO.Path.Combine(localAppData, "ftx-mcp", "bridges");
    }

    private static string RegistryEntryPath(int port)
    {
        return System.IO.Path.Combine(RegistryDir(), port.ToString() + ".json");
    }

    // Returns the best available project_path string, or null.
    // Tries Project.Current first (no FilePath property is exposed in the
    // current SDK version - BrowseName is the only name-like field used
    // throughout this file), so falls back to Environment.CurrentDirectory
    // (the directory Studio was launched from / opened the .optix file from).
    private static string GetProjectPath()
    {
        try
        {
            // Attempt 1: reflection probe for a FilePath or StorePath
            // property that may exist in newer SDK drops without needing
            // a compile-time reference to the property name.
            var p = Project.Current;
            if (p != null)
            {
                foreach (var name in new[] { "FilePath", "StorePath", "ProjectFilePath" })
                {
                    try
                    {
                        var pi = p.GetType().GetProperty(name,
                            BindingFlags.Public | BindingFlags.Instance | BindingFlags.FlattenHierarchy);
                        if (pi != null)
                        {
                            var val = pi.GetValue(p) as string;
                            if (!string.IsNullOrEmpty(val)) return val;
                        }
                    }
                    catch { /* property not present in this SDK version */ }
                }
            }
        }
        catch { /* Project.Current unavailable */ }
        // Attempt 2: Studio's working directory at launch - typically the
        // directory containing the .optix file that was opened.
        try
        {
            var cwd = Environment.CurrentDirectory;
            if (!string.IsNullOrEmpty(cwd)) return cwd;
        }
        catch { }
        return null;
    }

    /// <summary>
    /// Writes the registry entry JSON for <paramref name="port"/> to
    /// %LOCALAPPDATA%\ftx-mcp\bridges\&lt;port&gt;.json. Best-effort:
    /// any I/O failure is logged as a warning and swallowed so a missing
    /// LOCALAPPDATA or a permissions error never prevents the bridge starting.
    /// </summary>
    private static void WriteRegistryEntry(int port)
    {
        try
        {
            string dir = RegistryDir();
            System.IO.Directory.CreateDirectory(dir);
            string project = "unknown";
            string projectPath = null;
            try
            {
                var p = Project.Current;
                if (p != null) project = p.BrowseName;
            }
            catch { }
            projectPath = GetProjectPath();
            int pid = System.Diagnostics.Process.GetCurrentProcess().Id;
            string startedAt = DateTime.UtcNow.ToString("o", System.Globalization.CultureInfo.InvariantCulture);
            // Build JSON without taking a dependency on Newtonsoft / System.Text.Json
            // (neither is guaranteed in the Studio NetLogic sandbox).
            string projectPathJson = projectPath == null ? "null" : "\"" + JsonEscape(projectPath) + "\"";
            string json =
                "{\"port\":" + port +
                ",\"project\":\"" + JsonEscape(project) + "\"" +
                ",\"project_path\":" + projectPathJson +
                ",\"pid\":" + pid +
                ",\"bridge_version\":\"" + BridgeVersion + "\"" +
                ",\"started_at\":\"" + JsonEscape(startedAt) + "\"}";
            System.IO.File.WriteAllText(RegistryEntryPath(port), json, Encoding.UTF8);
        }
        catch (Exception ex)
        {
            Log.Warning("StudioBridge", "registry write failed for port " + port + ": " + ex.Message);
        }
    }

    /// <summary>
    /// Deletes the registry entry for <paramref name="port"/> if it exists.
    /// Best-effort: any I/O failure is silently swallowed so a missing file
    /// or permissions error never prevents the bridge stopping cleanly.
    /// </summary>
    private static void DeleteRegistryEntry(int port)
    {
        try
        {
            string path = RegistryEntryPath(port);
            if (System.IO.File.Exists(path)) System.IO.File.Delete(path);
        }
        catch { /* ignore */ }
    }

    // tries each port in BasePort..BasePort+PortRangeSize-1 in turn
    // and binds the first free one, instead of exclusively owning a single fixed
    // port. This is what lets PortRangeSize Studio instances each run their own
    // armed bridge simultaneously (no more manual StopBridge-on-one-to-free-it-
    // for-another). The bound port is recorded in BoundPortEnvVar (process-scoped,
    // survives the ALC reload) so a later StopBridge call - which runs in a fresh
    // ALC with none of this method's static state - can recover exactly which
    // port THIS Studio instance is using and signal only that one.
    // binds `port` with a few short retries before giving
    // up on it. Absorbs the window between a StopBridge signal and the OLD
    // listener thread actually noticing it and closing its socket (Loop polls
    // the stop event roughly every 50ms, longer if it's mid-HandleClient) - so
    // an ordinary "StopBridge, then StartBridge again a moment later" reclaims
    // ITS OWN port instead of creeping down the range because of a false "port
    // in use" from a teardown that just hadn't finished yet. Only retries on
    // SocketException (genuinely still bound / still tearing down); anything
    // else propagates immediately, unchanged from before this fix.
    private bool TryBindWithRetry(int port, out TcpListener listener, out SocketException bindError)
    {
        bindError = null;
        for (int attempt = 0; attempt < 4; attempt++)
        {
            try
            {
                var l = new TcpListener(IPAddress.Loopback, port);
                l.Start();               // exclusive bind (no SO_REUSEADDR - a clear
                                         // "in use" beats a silent double-bind).
                listener = l;
                return true;
            }
            catch (SocketException ex)
            {
                bindError = ex;
                if (attempt < 3) Thread.Sleep(150);   // ~450ms worst case before this port's given up on
            }
        }
        listener = null;
        return false;
    }

    private void StartListener(string via)
    {
        for (int port = BasePort; port < BasePort + PortRangeSize; port++)
        {
            // Clear any prior stop-signal for THIS port so a fresh loop on it
            // doesn't exit immediately.
            try { using (var ev = OpenStopEvent(port)) ev.Reset(); } catch { /* ignore */ }
            TcpListener listener;
            SocketException bindError;
            bool bound;
            try
            {
                bound = TryBindWithRetry(port, out listener, out bindError);
            }
            catch (Exception ex)
            {
                _running = false; _listener = null; _boundPort = -1;
                Log.Error("StudioBridge", "failed to start on port " + port + ": " + ex.Message);
                return;
            }
            if (!bound)
            {
                _running = false; _listener = null; _boundPort = -1;
                continue;   // still taken after the retries above; try the next port in the range
            }
            _listener = listener;
            _boundPort = port;
            // ADD to the process-wide port list rather than
            // overwriting it - see BoundPortEnvVar's comment. A prior port left
            // bound by an orphaned/edited-and-rebuilt listener stays remembered
            // here until ITS OWN Loop() thread removes it (or a StopBridge sweep
            // signals it to exit and it removes itself), instead of being silently
            // forgotten the moment this new port gets recorded.
            AddBoundPort(port);
            WriteRegistryEntry(port);
            _running = true;
            new Thread(() => Loop(port)) { IsBackground = true, Name = "StudioBridge" }.Start();
            Log.Info("StudioBridge", "listening on http://127.0.0.1:" + port +
                     " (started via " + via + ")");
            return;
        }
        Log.Error("StudioBridge", "cannot bind any port in " + BasePort + "-" +
            (BasePort + PortRangeSize - 1) + " - " + PortRangeSize +
            " bridges are already running, or a stale one holds a port. StopBridge now sweeps " +
            "EVERY port this process has ever bound (not just the most recent), so clicking it " +
            "once more should free any orphan left by an edited-and-rebuilt NetLogic - if it " +
            "still doesn't, close+reopen this Studio to drop a listener from before this fix was " +
            "applied. Raise PortRangeSize in StudioMCPBridge.cs if you routinely need more than " +
            PortRangeSize + " simultaneous bridges.");
    }

    private void StopListener() { StopListener(false); }

    private void StopListener(bool quiet)
    {
        // Recover EVERY port THIS Studio process's bridge is (or might still be)
        // bound to. Can't read _boundPort here - StopBridge runs in a fresh ALC
        // where that static was never set - so read the process-env var
        // StartListener/AddBoundPort maintain instead (see BoundPortEnvVar).
        //
        // sweep ALL of them, not just one - a NetLogic
        // recompile (edit this file, rebuild, StartBridge again) leaves the
        // PREVIOUS compiled listener thread alive on its own port, which used to
        // be silently forgotten the moment the new port overwrote this value.
        // Signalling every recorded port's named stop-event means one StopBridge
        // click cleans up all of them - including that kind of orphan - without
        // needing Studio closed.
        var ports = ReadBoundPorts();
        if (ports.Count > 0)
        {
            foreach (var port in ports)
            {
                // The cross-ALC signal (the only thing that reliably reaches each
                // running loop, whichever ALC generation it belongs to).
                try { using (var ev = OpenStopEvent(port)) ev.Set(); }
                catch (Exception ex) { Log.Warning("StudioBridge", "stop-signal failed for port " + port + ": " + ex.Message); }
                // Best-effort registry cleanup - delete eagerly here so the file
                // disappears as soon as StopBridge is clicked, even before the
                // Loop() thread has noticed the stop event and exited. Loop's
                // finally calls DeleteRegistryEntry too (belt-and-suspenders) in
                // case the process dies before this path runs.
                DeleteRegistryEntry(port);
            }
            if (!quiet) Log.Info("StudioBridge", "stop signalled (port(s) " + string.Join(", ", ports) + " releasing)");
        }
        else if (!quiet)
        {
            Log.Info("StudioBridge", "stop signalled (no bridge port on record for this " +
                "instance - was it ever started via StartBridge?)");
        }
        // Best-effort same-ALC teardown too (harmless when state isn't shared).
        _running = false;
        var l = _listener; _listener = null; _boundPort = -1;
        try { l?.Stop(); } catch { /* ignore */ }
        // NOTE: do NOT blank-clear BoundPortEnvVar here. Each Loop() thread removes
        // ONLY ITS OWN port (see Loop's finally / RemoveBoundPort) once it actually
        // exits, so the list stays accurate for any port that hasn't finished
        // tearing down yet by the time this method returns, instead of being wiped
        // out from under it.
    }

    // NOTE: main-thread marshaling via DelayedTask(0, node) was tried and REMOVED -
    // at DESIGN TIME Studio does not pump the async-task queue, so the task never
    // runs and a blocking Wait() HANGS the listener (confirmed live). The
    // fresh-instance materialization instead uses node.GetOrCreateVariable (node-model
    // ops, off-thread-safe) - see SetPropertyInline. No marshaling is needed.

    // Non-blocking accept loop: poll the named stop-event AND Pending() each ~50ms so
    // the loop stays responsive to a StopBridge from another ALC (a blocking
    // AcceptTcpClient could only be broken by our own ALC's Stop(), which StopBridge
    // can't reach). On stop, close the listener so the port is freed.
    // takes `port` as a parameter (closed over by the lambda
    // StartListener spawns this thread with) rather than reading the static
    // _boundPort - Loop always runs within the SAME ALC/thread epoch as the
    // StartListener call that spawned it, so either would work here, but the
    // parameter makes that independence from ALC-surviving state explicit and
    // matches OpenStopEvent's new per-port signature.
    private void Loop(int port)
    {
        EventWaitHandle stopEv = null;
        try { stopEv = OpenStopEvent(port); } catch { /* poll _running only */ }
        try
        {
            while (_running)
            {
                if (stopEv != null && stopEv.WaitOne(0)) break;   // StopBridge signalled
                var lst = _listener;
                if (lst == null) break;
                bool pending;
                try { pending = lst.Pending(); }
                catch { break; }                                  // listener disposed
                if (!pending) { Thread.Sleep(50); continue; }
                TcpClient client = null;
                // I31: ACCEPT and HANDLE are separate try blocks. They used to
                // share one, so a SocketException raised while ANSWERING a request
                // (a client that hung up mid-write) hit the `break` meant for a
                // disposed listener and took the whole bridge down with it. No
                // handler fault may end the accept loop - that is half of I31.
                try { client = lst.AcceptTcpClient(); }
                catch (SocketException) { break; }                 // listener disposed
                catch (Exception ex)
                {
                    Log.Warning("StudioBridge", "accept error: " + ExcMsg(ex));
                    continue;
                }
                try { HandleClient(client); }
                catch (Exception ex) { Log.Warning("StudioBridge", "request error: " + ExcMsg(ex)); }
                finally { try { client?.Close(); } catch { /* ignore */ } }
            }
        }
        finally
        {
            _running = false;
            try { _listener?.Stop(); } catch { /* ignore */ }
            _listener = null;
            _boundPort = -1;
            // remove ONLY this port from the shared list, not
            // the whole thing - a blanket clear here would erase the record of any
            // OTHER port this process still has a live (or still-orphaned) listener
            // on, which is exactly the bug this release fixes. See BoundPortEnvVar's
            // comment and RemoveBoundPort.
            try { RemoveBoundPort(port); } catch { /* ignore */ }
            // Delete the registry file for this port. StopListener already
            // deletes it eagerly on a normal stop; this covers the crash /
            // abnormal-exit case where StopListener never ran.
            DeleteRegistryEntry(port);
            try { stopEv?.Dispose(); } catch { /* ignore */ }
            Log.Info("StudioBridge", "listener loop exited; port " + port + " released");
        }
    }

    private void HandleClient(TcpClient client)
    {
        using (var stream = client.GetStream())
        {
            var buf = new byte[4096];
            int n = stream.Read(buf, 0, buf.Length);
            string req = n > 0 ? Encoding.ASCII.GetString(buf, 0, n) : "";
            string firstLine = req.Split('\n').FirstOrDefault() ?? "";

            string body;
            string status;
            try
            {
                if (firstLine.StartsWith("GET /bridge/health"))
                {
                    body = HealthJson();
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/nodes"))
                {
                    string path = QueryParam(firstLine, "path");
                    if (string.IsNullOrEmpty(path))
                    {
                        body = ErrorJson("bad_query", "missing required query param: path");
                        status = "400 Bad Request";
                    }
                    else
                    {
                        var node = ResolveNode(path);
                        if (node == null)
                        {
                            body = ErrorJson("node_not_found", "no node at path: " + path);
                            status = "404 Not Found";
                        }
                        else
                        {
                            body = NodeJson(path, node);
                            status = "200 OK";
                        }
                    }
                }
                else if (firstLine.StartsWith("GET /bridge/map"))
                {
                    string mPath = QueryParam(firstLine, "path");
                    int mDepth = 3, mMax = 800;
                    int.TryParse(QueryParam(firstLine, "depth") ?? "", out mDepth);
                    if (mDepth <= 0) mDepth = 3;
                    int.TryParse(QueryParam(firstLine, "max") ?? "", out mMax);
                    if (mMax <= 0) mMax = 800;
                    bool mIds = (QueryParam(firstLine, "ids") ?? "0") == "1";
                    string mMode = QueryParam(firstLine, "mode") ?? "detail";
                    string mMatch = QueryParam(firstLine, "match");
                    body = string.IsNullOrEmpty(mMatch)
                        ? ProjectMapJson(mPath, mDepth, mMax, mIds, mMode)
                        : MapSearchJson(mPath, mMatch, mMax);
                    if (body == null)
                    {
                        body = ErrorJson("node_not_found", "no node at path: " + mPath);
                        status = "404 Not Found";
                    }
                    else status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/screens"))
                {
                    body = ScreensJson();
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/types/ui"))
                {
                    body = TypesUiJson();
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/schema/dump"))
                {
                    body = SchemaDumpJson();
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/types/schema"))
                {
                    string typeName = QueryParam(firstLine, "type");
                    if (string.IsNullOrEmpty(typeName))
                    {
                        body = ErrorJson("bad_query", "missing required query param: type");
                        status = "400 Bad Request";
                    }
                    else
                    {
                        body = TypeSchemaJson(typeName);
                        status = body == null ? "404 Not Found" : "200 OK";
                        if (body == null)
                            body = ErrorJson("type_not_found", "no builtin UI type: " + typeName);
                    }
                }
                else if (firstLine.StartsWith("GET /bridge/node/typeinfo"))
                {
                    string tp = QueryParam(firstLine, "path");
                    if (string.IsNullOrEmpty(tp))
                    {
                        body = ErrorJson("bad_query", "missing required query param: path");
                        status = "400 Bad Request";
                    }
                    else { body = TypeInfoJson(tp); status = "200 OK"; }
                }
                else if (firstLine.StartsWith("POST /bridge/node/reorder"))
                {
                    body = ReorderInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/attach-expression"))
                {
                    body = AttachExpressionInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/attach-string-formatter"))
                {
                    body = AttachStringFormatterInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/attach-formatter"))
                {
                    body = AttachFormatterInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/retype"))
                {
                    body = RetypeVariableInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/node/varmembers"))
                {
                    body = VarMembersJson(QueryParam(firstLine, "path"), QueryParam(firstLine, "name"));
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("GET /bridge/diag/clrtype"))
                {
                    body = DiagClrTypeJson(QueryParam(firstLine, "name"));
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/validate_ops"))
                {
                    // U16: the ONLY handler that takes a POST BODY. Every other
                    // write route passes params on the query string (see
                    // WriteVariableInline's note) - an op LIST does not fit
                    // there, so this one reads the body after the headers.
                    body = ValidateOpsJson(ReadRequestBody(stream, buf, n, req));
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/expr/validate"))
                {
                    body = ValidateExprJson(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/model/variable"))
                {
                    // Create a Model variable inline on the HTTP thread (node-model
                    // ops are off-thread-safe at design time; no marshaling).
                    body = WriteVariableInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/model/folder"))
                {
                    body = CreateFolderInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/model/object"))
                {
                    body = CreateObjectInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/model/netlogic"))
                {
                    body = CreateNetLogicInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/model/type"))
                {
                    body = CreateTypeInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/convert-to-type"))
                {
                    body = ConvertToTypeInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/move"))
                {
                    body = MoveNodeInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/ui/widget"))
                {
                    // Add a UI object to a screen inline (touches the presentation
                    // engine; still node-model, off-thread-safe).
                    body = WriteWidgetInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/displayname"))
                {
                    // DisplayName ATTRIBUTE set - deliberately its own route, not a
                    // set_property branch: attributes are not UA child variables and
                    // must never touch the GetOrCreateVariable path (orphan crash).
                    body = SetDisplayNameInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/property"))
                {
                    // GENERIC property set - NOT per-component plumbing. Every
                    // property is an IUAVariable (node.GetVariable(name)); set
                    // .Value coerced by the property's own DataType. LocalizedText
                    // is just one coercion branch alongside Bool/Int/Double/String.
                    body = SetPropertyInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/bind"))
                {
                    // Bind a property -> model variable (DynamicLink). Node-model op.
                    body = BindPropertyInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/alias"))
                {
                    body = CreateAliasInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/i18n/translation"))
                {
                    body = AddTranslationInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/delete"))
                {
                    body = DeleteNodeInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/node/event"))
                {
                    body = WireEventInline(firstLine);
                    status = "200 OK";
                }
                // new dispatcher branch, paired with
                // InvokeMethodInline() below -- the generic UAMethod-invoke endpoint.
                else if (firstLine.StartsWith("POST /bridge/node/invoke"))
                {
                    body = InvokeMethodInline(firstLine);
                    status = "200 OK";
                }
                else if (firstLine.StartsWith("POST /bridge/setup/web-engine"))
                {
                    // Ensure a Web presentation engine exists so the runtime serves a
                    // canvas (the manual "add Web presentation engine" setup step).
                    body = EnsureWebEngineInline(firstLine);
                    status = "200 OK";
                }
                else
                {
                    body = ErrorJson("not_found", "unknown route");
                    status = "404 Not Found";
                }
            }
            // I31: the dispatch-wide net. ANY exception out of ANY route becomes
            // one rejected request (500 + the standard error body), never a lost
            // Studio session. ExcMsg carries the exception TYPE as well as the
            // message - "__probe__ is not a valid value" says much less than
            // "ArgumentException: __probe__ is not a valid value". What a managed
            // catch CANNOT stop is a corrupted-state/native abort (the
            // StringFormatter.Format setter, the array-write class, the enum
            // string-assign assert): each of those has its own up-front guard,
            // because by the time one fires there is no handler to return from.
            catch (Exception ex)
            {
                body = ErrorJson("internal", ExcMsg(ex));
                status = "500 Internal Server Error";
            }

            // U21: one Output line per MUTATION (never for reads -- they burst).
            // Sited here, after the dispatch chain and the catch, because this
            // is the single point where every route's FINAL body passes on its
            // way to the one WriteResponse -- including the internal-error path.
            MaybeLogMutation(firstLine, body);

            WriteResponse(stream, status, body);
        }
    }

    // ---- U21: per-mutation Output logging -----------------------------------

    // Routes that MUTATE the model -- the only ones that log. Keep in sync with
    // the POST branches in the dispatcher (model/*, ui/widget, i18n/translation,
    // setup/web-engine, and node/{property,bind,alias,move,convert-to-type,
    // reorder,delete,event,attach-expression}). Read routes are deliberately
    // absent, and so is POST /bridge/expr/validate -- a POST by shape but a pure
    // read (it validates, mutates nothing).
    private static readonly string[] _MutationRoutes = {
        "POST /bridge/model/", "POST /bridge/ui/widget",
        "POST /bridge/i18n/translation", "POST /bridge/setup/web-engine",
        "POST /bridge/node/property", "POST /bridge/node/bind",
        "POST /bridge/node/alias", "POST /bridge/node/move",
        "POST /bridge/node/convert-to-type", "POST /bridge/node/reorder",
        "POST /bridge/node/delete", "POST /bridge/node/event",
        "POST /bridge/node/attach-expression",
        "POST /bridge/node/attach-formatter",
        "POST /bridge/node/attach-string-formatter",
        "POST /bridge/node/retype",
        // added for the generic invoke endpoint below.
        "POST /bridge/node/invoke",
    };

    private void MaybeLogMutation(string firstLine, string body)
    {
        try
        {
            bool isMutation = false;
            foreach (var r in _MutationRoutes)
                if (firstLine.StartsWith(r)) { isMutation = true; break; }
            if (!isMutation) return;
            // ErrorJson (and the did_you_mean builder) always start
            // {"error":{"code":... ; anything else is a success body
            // (PropOkJson / create-node builders).
            bool ok = !string.IsNullOrEmpty(body) && !body.StartsWith("{\"error\"");
            Log.Info("StudioBridge", (ok ? "OK " : "FAIL ") + OpLabel(firstLine));
        }
        catch { /* logging must NEVER break a write */ }
    }

    private string OpLabel(string firstLine)
    {
        // "<VERB> <route>" + path=/name= query params. NEVER the `value` param --
        // it can be large and is untrusted content.
        var parts = firstLine.Split(' ');
        string route = parts.Length >= 2 ? parts[0] + " " + parts[1].Split('?')[0]
                                         : firstLine;
        var sb = new StringBuilder(route);
        string p = QueryParam(firstLine, "path");
        string nm = QueryParam(firstLine, "name");
        if (!string.IsNullOrEmpty(p)) sb.Append(" path=").Append(p);
        if (!string.IsNullOrEmpty(nm)) sb.Append(" name=").Append(nm);
        return sb.ToString();
    }

    // ---- endpoint bodies ----------------------------------------------------

    private string HealthJson()
    {
        string project = "unknown";
        bool modelLoaded = false;
        try
        {
            var p = Project.Current;
            if (p != null) { project = p.BrowseName; modelLoaded = true; }
        }
        catch (Exception ex)
        {
            Log.Warning("StudioBridge", "Project.Current unavailable: " + ex.Message);
        }
        string projectPath = GetProjectPath();
        int pid = System.Diagnostics.Process.GetCurrentProcess().Id;
        // "port" lets the Python service, which now probes a whole
        // port RANGE (see StartListener), confirm which port answered rather
        // than assuming the well-known 8768 - and is handy for a human reading
        // the raw JSON while debugging which Studio instance is which.
        // "project_path" and "pid" are the two new fields added for the
        // registry-file feature (1.0.8) so the Python service can resolve
        // project paths and pids without a second OS process scan.
        string projectPathJson = projectPath == null ? "null" : "\"" + JsonEscape(projectPath) + "\"";
        return "{\"bridge_version\":\"" + BridgeVersion +
               "\",\"project\":\"" + JsonEscape(project) +
               "\",\"model_loaded\":" + Bool(modelLoaded) +
               ",\"port\":" + _boundPort +
               ",\"project_path\":" + projectPathJson +
               ",\"pid\":" + pid +
               // invoke_session tells the service this build runs /bridge/node/invoke
               // under a root session (see InvokeMethodInline). Builds without it
               // run ExecuteMethod session-less, which kills Studio; the service
               // refuses invoke against them.
               ",\"invoke_session\":\"root\"}";
    }

    private string NodeJson(string path, IUANode node)
    {
        var children = new StringBuilder();
        var props = new StringBuilder();
        int childCount = 0, propCount = 0;
        bool childTrunc = false, propTrunc = false;

        foreach (var child in node.Children)
        {
            if (child is IUAVariable v)
            {
                if (propCount >= MaxItems) { propTrunc = true; continue; }
                if (propCount++ > 0) props.Append(",");
                props.Append("{\"name\":\"" + JsonEscape(child.BrowseName) +
                             "\",\"datatype\":\"" + JsonEscape(DataTypeNameFull(v)) +
                             "\",\"value\":\"" + JsonEscape(ValueString(v)) + "\"}");
            }
            else
            {
                if (childCount >= MaxItems) { childTrunc = true; continue; }
                if (childCount++ > 0) children.Append(",");
                children.Append("{\"browse_name\":\"" + JsonEscape(child.BrowseName) +
                                "\",\"node_class\":\"" + child.NodeClass +
                                "\",\"dotnet_type\":\"" + JsonEscape(DotNetTypeName(child)) + "\"}");
            }
        }

        return "{\"path\":\"" + JsonEscape(path) +
               "\",\"browse_name\":\"" + JsonEscape(node.BrowseName) +
               "\",\"node_class\":\"" + node.NodeClass +
               "\",\"dotnet_type\":\"" + JsonEscape(DotNetTypeName(node)) +
               "\",\"children\":[" + children + "]" +
               ",\"properties\":[" + props + "]" +
               ",\"truncated\":" + Bool(childTrunc || propTrunc) + "}";
    }

    // I35 (2026-09-04, PopupApp2). node.GetType().Name is the generated CLR PROXY
    // name, and for a project ObjectType with no UI base the proxy is NOT that
    // node's kind: `create_type` with no base makes a plain BaseObjectType (the
    // bridge says so - CreateTypeInline's baseLabel), yet describe_node reported
    // dotnet_type "ScreenType" on the type and "Screen" on its instance. Not a
    // cosmetic mislabel - it steered a diagnosis the wrong way for several turns
    // while the REAL fault was that a bare-typed instance inside a Screen makes the
    // WEB presentation throw "Object type: 8/93 is not an UI object type" and render
    // a blank page.
    //
    // So corroborate the proxy name against the node's REAL type chain - the
    // SuperType chain for a type, the ObjectType chain for an instance - and keep
    // the proxy name only when the chain agrees with it. A real Screen/Panel type
    // is corroborated ("Screen" in the chain vs proxy "ScreenType") and its output
    // is unchanged; a bare type is not, and reports what the chain actually says:
    // BaseObjectType for the type, BaseObject for its instance.
    //
    // Deliberately NOT applied to the map walkers (MapNodeJson/MapSearch): they
    // emit a proxy name per node over hundreds of nodes, where the chain walk is
    // real cost and the label is a coarse filter, not a diagnosis.
    private static string DotNetTypeName(IUANode node)
    {
        string clr = node.GetType().Name;
        try
        {
            bool isType = node is IUAObjectType;
            IUAObjectType cur = isType ? ((IUAObjectType)node).SuperType
                                       : (node as UAObject)?.ObjectType;
            var chain = new List<string>();
            for (int i = 0; cur != null && i < 20; i++, cur = cur.SuperType)
                chain.Add(cur.BrowseName);
            if (chain.Count == 0) return clr;      // variables, roots - nothing to check against
            foreach (var b in chain)
                if (b == clr || b + "Type" == clr || b == clr + "Type") return clr;
            string root = chain[chain.Count - 1];
            if (!isType && root.EndsWith("Type", StringComparison.Ordinal) && root.Length > 4)
                root = root.Substring(0, root.Length - 4);   // BaseObjectType -> BaseObject
            return root;
        }
        catch { return clr; }
    }

    // Parity with the file-path list_screens (optix_model.SCREEN_TYPES) plus the
    // window type the live model reports (MainWindow -> WindowType, validated
    // Calibrate against real screen/dialog nodes on the next Studio run.
    private static readonly string[] ScreenTypes = { "Screen", "Panel", "Dialog", "WindowType" };

    private string ScreensJson()
    {
        var sb = new StringBuilder();
        int count = 0;
        bool trunc = false;
        var ui = ResolveNode("UI");
        if (ui != null)
            CollectScreens(ui, "UI", sb, ref count, ref trunc, 0);
        return "{\"screens\":[" + sb + "],\"count\":" + count +
               ",\"truncated\":" + Bool(trunc) + "}";
    }

    private void CollectScreens(IUANode node, string path, StringBuilder sb,
                               ref int count, ref bool trunc, int depth)
    {
        if (depth > 5) return;
        foreach (var child in node.Children)
        {
            string childPath = path + "/" + child.BrowseName;
            string tn = child.GetType().Name;
            if (Array.IndexOf(ScreenTypes, tn) >= 0)
            {
                if (count >= MaxItems) { trunc = true; return; }
                if (count++ > 0) sb.Append(",");
                sb.Append("{\"name\":\"" + JsonEscape(child.BrowseName) +
                          "\",\"type\":\"" + JsonEscape(tn) +
                          "\",\"node_class\":\"" + child.NodeClass +
                          "\",\"path\":\"" + JsonEscape(childPath) +
                          "\",\"child_count\":" + child.Children.Count() + "}");
            }
            // Recurse into folders (e.g. UI/Screens) to find nested screens.
            if (tn == "Folder")
                CollectScreens(child, childPath, sb, ref count, ref trunc, depth + 1);
        }
    }

    // Enumerate the builtin UI type catalog by reflecting FTOptix.UI.ObjectTypes
    // (NodeId constants) and resolving each to its model type. This is the
    // resource-map / type-discovery surface - the answer to "what controls
    // exist?" without the model guessing. NOTE: ObjectTypes also carries event
    // types (MouseClickEvent, ...); a future pass could filter to BaseUIObject
    // subtypes.
    private string TypesUiJson()
    {
        var sb = new StringBuilder();
        int count = 0;
        bool trunc = false;
        var fields = typeof(FTOptix.UI.ObjectTypes)
            .GetFields(BindingFlags.Public | BindingFlags.Static);
        foreach (var f in fields)
        {
            if (count >= MaxItems) { trunc = true; break; }
            string browse = f.Name;
            bool viaBrowseName = false;
            try
            {
                // Route through the SAME resolver create_widget/describe_type use, so
                // an entry whose compiled NodeId constant points at another node is
                // CORRECTED here rather than reported as another type's browse name
                // (the VirtualKeyboard* block: 8/1398 resolves to the DataType
                // VirtualKeyboardTypeEnum in the loaded FTOptix.UI module). Only a
                // name the browse-name walk cannot find at all keeps the stale
                // constant's browse name - and then WITHOUT resolved_by, which is
                // the honest signal that nothing was reconciled.
                var nid = ResolveUiTypeId(f.Name, out viaBrowseName);
                if (nid == null)
                {
                    viaBrowseName = false;
                    if (f.GetValue(null) is NodeId stale)
                    {
                        var st = InformationModel.Get(stale);
                        if (st != null) browse = st.BrowseName;
                    }
                }
                else
                {
                    var t = InformationModel.Get(nid);
                    if (t != null) browse = t.BrowseName;
                }
            }
            catch { /* unresolved type id - fall back to the field name */ }
            if (count++ > 0) sb.Append(",");
            sb.Append("{\"name\":\"" + JsonEscape(f.Name) +
                      "\",\"browse_name\":\"" + JsonEscape(browse) + "\"" +
                      (viaBrowseName ? ",\"resolved_by\":\"browse_name\"" : "") + "}");
        }
        return "{\"types\":[" + sb + "],\"count\":" + count +
               ",\"truncated\":" + Bool(trunc) + "}";
    }

    // GET /bridge/schema/dump - the WHOLE builtin type catalog x per-type property
    // reflection, in one call, so the service can cache it offline (keyed by Studio
    // version) and diff it across Studio upgrades.
    //
    // Deliberately NOT capped at MaxItems, unlike TypesUiJson/TypeSchemaJson: those
    // serve a human/LLM reading one answer, where 500 is a sane ceiling. A dump that
    // silently dropped types or properties would cache as a schema that looks
    // complete and would then show up as phantom additions/removals in the next
    // version diff. Size is fine - WriteResponse sets a real Content-Length and
    // writes the whole body.
    //
    // Reuses the same two reflectors as the single-type routes so the dump can never
    // disagree with describe_type: the ObjectTypes field set for the catalog, and the
    // ResolveWidgetClrType + IsLegendProp loop for properties. Emits only the three
    // contract fields {name, datatype, settable}; the placeholder-collection extras
    // TypeSchemaJson adds are omitted (the Python schema_diff ignores unknown keys,
    // so the narrow shape is the safer default).
    //
    // NOTE: ObjectTypes also carries EVENT types (MouseClickEvent...), which appear
    // in the dump with an empty property list. Harmless for diffing; filtering to
    // BaseUIObject subtypes is a future nicety.
    private string SchemaDumpJson()
    {
        string version = "unknown";
        try
        {
            var v = typeof(FTOptix.UI.ObjectTypes).Assembly.GetName().Version;
            if (v != null && !string.IsNullOrEmpty(v.ToString())) version = v.ToString();
        }
        catch { /* keep "unknown" - the Python _version_key sanitizes whatever it gets */ }

        var types = new StringBuilder();
        int typeCount = 0;
        var fields = typeof(FTOptix.UI.ObjectTypes)
            .GetFields(BindingFlags.Public | BindingFlags.Static);
        foreach (var f in fields)
        {
            // Per-type guard: one unresolvable type must not abort the whole dump.
            try
            {
                string browse = f.Name;
                bool viaBrowseName = false;
                // Same resolver as TypesUiJson/TypeSchemaJson so the dump can never
                // disagree with describe_type about a type's browse name.
                var nid = ResolveUiTypeId(f.Name, out viaBrowseName);
                if (nid == null)
                {
                    viaBrowseName = false;
                    if (f.GetValue(null) is NodeId stale)
                    {
                        var st = InformationModel.Get(stale);
                        if (st != null) browse = st.BrowseName;
                    }
                }
                else
                {
                    var t = InformationModel.Get(nid);
                    if (t != null) browse = t.BrowseName;
                }

                var props = new StringBuilder();
                int propCount = 0;
                var clr = ResolveWidgetClrType(f.Name);
                if (clr != null)
                {
                    foreach (var pi in clr.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                                 .Where(IsLegendProp)
                                 .GroupBy(p => p.Name).Select(g => g.First()).OrderBy(p => p.Name))
                    {
                        if (propCount++ > 0) props.Append(",");
                        props.Append("{\"name\":\"" + JsonEscape(pi.Name) +
                                     "\",\"datatype\":\"" + JsonEscape(pi.PropertyType.Name) +
                                     "\",\"settable\":" +
                                     Bool(pi.CanWrite && !pi.PropertyType.IsArray) + "}");
                    }
                }

                if (typeCount++ > 0) types.Append(",");
                types.Append("\"" + JsonEscape(f.Name) + "\":{\"browse_name\":\"" +
                             JsonEscape(browse) + "\"" +
                             (viaBrowseName ? ",\"resolved_by\":\"browse_name\"" : "") +
                             ",\"properties\":[" + props + "]}");
            }
            catch { /* skip this type, keep dumping the rest */ }
        }

        return "{\"studio_version\":\"" + JsonEscape(version) +
               "\",\"generated_at\":\"" + JsonEscape(DateTime.UtcNow.ToString("o")) +
               "\",\"types\":{" + types + "}}";
    }

    // Property schema of a builtin UI type, e.g. ?type=Label. Returns null
    // (-> 404) for an unknown type. Inheritance-COMPLETE: reflects the generated
    // Optix CLR proxy (FTOptix.UI.<Type>), whose properties include everything
    // inherited from Item/base - so a Panel correctly shows NO Border* and a
    // Rectangle does. This is the authoritative legend an author consults BEFORE a
    // set, so it never guesses a property the type lacks (the class of write that
    // crashes Studio - see the validity gate in the set-property path). Falls back
    // to the type node's direct IUAVariable children if the CLR type can't resolve.
    private string TypeSchemaJson(string typeName)
    {
        bool viaBrowseName;
        var nid = ResolveUiTypeId(typeName, out viaBrowseName);
        if (nid == null) return null;
        var t = InformationModel.Get(nid);
        if (t == null) return null;

        var props = new StringBuilder();
        int count = 0;
        bool trunc = false;
        var clr = ResolveWidgetClrType(typeName);
        if (clr != null)
        {
            foreach (var pi in clr.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                         .Where(IsLegendProp)
                         .GroupBy(p => p.Name).Select(g => g.First()).OrderBy(p => p.Name))
            {
                if (count >= MaxItems) { trunc = true; break; }
                if (count++ > 0) props.Append(",");
                props.Append("{\"name\":\"" + JsonEscape(pi.Name) +
                             "\",\"datatype\":\"" + JsonEscape(pi.PropertyType.Name) +
                             // Array props (String[]/NodeId[]) are settable in CLR
                             // terms but set_property refuses them
                             // (unsupported_array_write) - report the tool truth.
                             "\",\"settable\":" + Bool(pi.CanWrite && !pi.PropertyType.IsArray));
                // Schema visibility for placeholder collections (spec sec.4): the
                // rule "children go IN the collection" is discoverable here
                // instead of via a failed emulator run.
                if (IsPlaceholderColl(pi.PropertyType) || IsPlaceholderRoColl(pi.PropertyType))
                {
                    var elem = PlaceholderElementType(pi.PropertyType);
                    props.Append(",\"placeholder_collection\":true" +
                                 ",\"collection_readonly\":" + Bool(IsPlaceholderRoColl(pi.PropertyType)) +
                                 (elem != null ? ",\"children_go_in\":\"" + JsonEscape(pi.Name) +
                                  "\",\"element_type\":\"" + JsonEscape(elem.Name) + "\"" : ""));
                }
                props.Append("}");
            }
        }
        else
        {
            foreach (var child in t.Children.OfType<IUAVariable>())
            {
                if (count >= MaxItems) { trunc = true; break; }
                if (count++ > 0) props.Append(",");
                props.Append("{\"name\":\"" + JsonEscape(child.BrowseName) +
                             "\",\"datatype\":\"" + JsonEscape(DataTypeNameFull(child)) + "\"}");
            }
        }
        return "{\"type\":\"" + JsonEscape(typeName) +
               "\",\"browse_name\":\"" + JsonEscape(t.BrowseName) + "\"" +
               (viaBrowseName ? ",\"resolved_by\":\"browse_name\"" : "") +
               ",\"properties\":[" + props + "]" +
               ",\"truncated\":" + Bool(trunc) + "}";
    }

    // Legend filter: an author-facing settable property. Keeps FTOptix-declared
    // value props, drops the noise the generated proxy also exposes - the IUAVariable
    // companion accessors (X has an XVariable pair) and structural children
    // (Children/GridLayoutProperties). Used ONLY for the human/LLM legend
    // (describe + the rejection hint), NOT for the gate's acceptance test - the gate
    // stays permissive so it never false-rejects a genuinely-declared property.
    private static bool IsLegendProp(System.Reflection.PropertyInfo pi)
    {
        return pi.DeclaringType != null && pi.DeclaringType.Namespace != null
            && pi.DeclaringType.Namespace.StartsWith("FTOptix")
            && !pi.Name.EndsWith("Variable")
            && pi.Name != "Children" && pi.Name != "GridLayoutProperties";
    }

    // GET /bridge/node/varmembers?path=X&name=Y - DIAGNOSTIC: dump a live variable's
    // runtime type + its members whose name looks access/permission-related. Used to
    // discover the correct OPC-UA read-only API (IUAVariable has no AccessLevel;
    // reflection off the concrete runtime type finds where it actually lives).
    private string VarMembersJson(string path, string name)
    {
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            IUAVariable v = string.IsNullOrEmpty(name) ? (node as IUAVariable) : node.GetVariable(name);
            if (v == null) return ErrorJson("not_a_variable", "no variable '" + name + "' on " + path);
            var rt = v.GetType();
            var sb = new StringBuilder();
            int i = 0;
            foreach (var mi in rt.GetMembers())
            {
                var n = mi.Name;
                if (n.IndexOf("Access", StringComparison.OrdinalIgnoreCase) < 0 &&
                    n.IndexOf("Permission", StringComparison.OrdinalIgnoreCase) < 0 &&
                    n.IndexOf("Writ", StringComparison.OrdinalIgnoreCase) < 0 &&
                    n.IndexOf("ReadOnly", StringComparison.OrdinalIgnoreCase) < 0 &&
                    n.IndexOf("Attribute", StringComparison.OrdinalIgnoreCase) < 0) continue;
                if (i++ > 0) sb.Append(",");
                sb.Append("{\"kind\":\"" + mi.MemberType + "\",\"name\":\"" + JsonEscape(n) + "\"}");
            }
            return "{\"path\":\"" + JsonEscape(path) + "\",\"name\":\"" + JsonEscape(name ?? "") +
                   "\",\"runtime_type\":\"" + JsonEscape(rt.FullName) +
                   "\",\"access_members\":[" + sb + "]}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // POST /bridge/node/attach-expression?path=<node>&name=<prop>&expression=<expr>&sources=<a,b,..>
    // Attach an ExpressionEvaluator converter to a property (roadmap tool A). The
    // ExpressionEvaluator is a formula language ("dumb Excel"): Expression is a
    // string with {0},{1},.. placeholders bound to SourceN inputs, e.g.
    // "if({0} > 40, 0xFFFF0000, 0xFF00FF00)" on a FillColor. Subsumes
    // ConditionalConverter/Linear/etc. Model shape + API from OptixMaster
    // SetInputVisibility.cs: MakeObject<ExpressionEvaluator>, set .Expression, make a
    // Source var per input with SetDynamicLink to the source, AddReference(HasSource),
    // then propVar.SetConverter(ee). SetDynamicLink is the same off-thread-safe call
    // bind_property already uses. Converters no-op SILENTLY if mis-wired -> runtime
    // render-verify, not just {ok:true}.
    private string AttachExpressionInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string name = QueryParam(firstLine, "name");
        string expr = QueryParam(firstLine, "expression");
        string sources = QueryParam(firstLine, "sources");
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name) || string.IsNullOrEmpty(expr))
            return ErrorJson("bad_query", "required: path, name, expression (+ sources=comma,sep,node,paths)");
        try
        {
            // Pre-validate the formula (Optix only checks at runtime -> a bad expr
            // silently no-ops). Same check as the ValidateExpression ExportMethod +
            // /bridge/expr/validate: reject the common syntactic mistakes up front.
            var exprErr = ValidateExpressionSyntax(expr, CountSources(sources));
            if (exprErr != null) return ErrorJson("bad_expression", exprErr);

            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            IUAVariable propVar = node.GetVariable(name);
            if (propVar == null)
            {
                var gate = DeclaredPropertyGuard(node, name);
                if (gate != null) return gate;
                // Same pre-materialization array gate as set_property: don't
                // GetOrCreateVariable an array-typed declared property (NodeId[]
                // materialization is implicated in the 2026-07-16 Studio crash).
                var arrGate = DeclaredArrayGuard(node, name);
                if (arrGate != null) return arrGate;
                propVar = (node as IUAObject)?.GetOrCreateVariable(name);
            }
            if (propVar == null) return ErrorJson("property_not_found", "no property " + name + " on " + path);

            var ee = InformationModel.MakeObject<FTOptix.CoreBase.ExpressionEvaluator>("ExpressionEvaluator");
            ee.Expression = expr;
            int i = 0;
            var added = new StringBuilder();
            // Kept so the Source links can be re-pointed AFTER SetConverter parents
            // the evaluator - see RelinkRelativeWithinType.
            var srcHolders = new List<IUAVariable>();
            var srcTargets = new List<IUANode>();
            if (!string.IsNullOrEmpty(sources))
            {
                foreach (var sp in sources.Split(','))
                {
                    var s = sp.Trim();
                    if (s.Length == 0) continue;
                    var srcVar = ResolveNode(s) as IUAVariable;
                    if (srcVar == null) return ErrorJson("source_not_variable", "source is not a variable: " + s);
                    var srcN = InformationModel.MakeVariable("Source" + i, OpcUa.DataTypes.BaseDataType);
                    srcN.SetDynamicLink(srcVar);
                    ee.Refs.AddReference(FTOptix.CoreBase.ReferenceTypes.HasSource, srcN);
                    srcHolders.Add(srcN);
                    srcTargets.Add(srcVar);
                    if (i > 0) added.Append(",");
                    added.Append("\"" + JsonEscape(s) + "\"");
                    i++;
                }
            }
            propVar.SetConverter(ee);
            // ONLY NOW is each Source variable in the tree (SetConverter parents the
            // evaluator), so only now can the SDK's absolute link be re-pointed at
            // the relative form. Detached, there was nothing to be relative to -
            // and an absolute source inside an ObjectType makes every instance read
            // the TYPE's variable, silently static (field finding 2026-09-04).
            var relSrc = new StringBuilder();
            int relCount = 0;
            for (int r = 0; r < srcHolders.Count; r++)
            {
                var rel = RelinkRelativeWithinType(srcHolders[r], srcTargets[r]);
                if (rel == null) continue;
                if (relCount++ > 0) relSrc.Append(",");
                relSrc.Append("\"" + JsonEscape(rel) + "\"");
            }
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path) + "\",\"name\":\"" + JsonEscape(name) +
                   "\",\"expression\":\"" + JsonEscape(expr) + "\",\"sources\":[" + added +
                   "],\"relative_sources\":[" + relSrc +
                   "],\"via\":\"expression-converter\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // POST /bridge/node/attach-string-formatter?path=<node>&name=<prop>&format=<fmt>&sources=<a,b,..>[&raw=1][&mode=Read]
    // Attach a StringFormatter as a converter ON THE PROPERTY itself (HasConverter
    // on the property, exactly where attach-expression puts its ExpressionEvaluator)
    // -- NOT on the DynamicLink. This is the shape that FORMATS A DISPLAY VALUE:
    //
    //   <prop>
    //     StringFormatter1          (StringFormatter)   <- HasConverter on the property
    //       Format = "{0:F1}"       (LocalizedText)     <- HasParameter
    //       Source0                 (BaseDataType)      <- HasSource
    //         DynamicLink -> <driver variable>
    //
    // attach-formatter (BuildFormattedLink) builds a DIFFERENT thing -- a formatted
    // dynamic LINK, where the StringFormatter hangs off the DynamicLink and formats
    // a NodePath. Used as a value formatter on Label.Text it renders EMPTY (field
    // finding 2026-09-01, re-confirmed 2026-09-16 on two bench projects).
    // The reference value-formatting shape was proven by hand in Studio (Label.Text
    // <- StringFormatter "{0:f1}" <- Float 12.222 rendered "12.2").
    //
    // Sources are POSITIONAL (Source0..N), so Format uses {0},{1},.. like an
    // expression. `raw=1` treats each source as a LITERAL NodePath (materialise a
    // Source holder with a raw DynamicLink value) so an alias/template path such as
    // "{data}/value" attaches on the TYPE and resolves per instance -- attach on a
    // resolvable variable otherwise. NEVER call StringFormatter.Format's property
    // SETTER: its generated setter reads Context.Sessions.CurrentSessionInfo, which
    // is null on this HTTP thread, so the native call AVs and kills Studio with
    // nothing logged (decompiled FTOptix.CoreBase.Net 2.1.0.89, 2026-09-16). Write
    // the Format HasParameter child directly, same as BuildFormattedLink does.
    // Highest positional placeholder index in a .NET composite format
    // ("{0:F2}", "{1,8:N1}"), or -1 when there is none. "{{" / "}}" are literal.
    private static int MaxPositionalPlaceholder(string format)
    {
        if (string.IsNullOrEmpty(format)) return -1;
        var stripped = format.Replace("{{", "").Replace("}}", "");
        int max = -1;
        foreach (System.Text.RegularExpressions.Match m in
                 System.Text.RegularExpressions.Regex.Matches(stripped, @"\{(\d+)\s*(?:,[^{}:]*)?(?::[^{}]*)?\}"))
        {
            int n;
            if (int.TryParse(m.Groups[1].Value, out n) && n > max) max = n;
        }
        return max;
    }

    private string AttachStringFormatterInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string name = QueryParam(firstLine, "name");
        string format = QueryParam(firstLine, "format");
        string sources = QueryParam(firstLine, "sources");
        string rawStr = QueryParam(firstLine, "raw");
        bool raw = rawStr == "1" || string.Equals(rawStr, "true", StringComparison.OrdinalIgnoreCase);
        string modeStr = QueryParam(firstLine, "mode") ?? "Read";
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name) || string.IsNullOrEmpty(format))
            return ErrorJson("bad_query",
                "required: path, name, format (+ sources=a,b for {0},{1}; raw=1 for literal NodePaths; mode=Read|ReadWrite|Write)");
        // Every {N} in format needs Source<N>. Without this a missing/misspelled
        // sources attached a formatter with no Source0 and answered ok:true; the
        // label rendered the bare format text (2026-09-24 live check).
        int boundCount = string.IsNullOrEmpty(sources) ? 0
            : sources.Split(',').Count(x => x.Trim().Length > 0);
        int maxPlaceholder = MaxPositionalPlaceholder(format);
        if (maxPlaceholder >= boundCount)
            return ErrorJson("unbound_placeholder",
                "format uses {" + maxPlaceholder + "} but only " + boundCount +
                " source(s) are bound - pass sources=<path>[,<path>..] (positional)");
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            IUAVariable propVar = node.GetVariable(name);
            if (propVar == null)
            {
                var gate = DeclaredPropertyGuard(node, name);
                if (gate != null) return gate;
                var arrGate = DeclaredArrayGuard(node, name);
                if (arrGate != null) return arrGate;
                if (node is IUAObject obj) propVar = obj.GetOrCreateVariable(name);
                else if (node is IUAObjectType objT) propVar = objT.GetOrCreateVariable(name);
            }
            if (propVar == null) return ErrorJson("property_not_found", "no property " + name + " on " + path);

            DynamicLinkMode mode;
            switch (modeStr)
            {
                case "Write": mode = DynamicLinkMode.Write; break;
                case "ReadWrite": mode = DynamicLinkMode.ReadWrite; break;
                default: mode = DynamicLinkMode.Read; break;
            }

            // Build the StringFormatter DETACHED, wire Format + Source<i>, then
            // attach as the property converter LAST -- same order as attach-formatter.
            var sf = InformationModel.MakeObject<StringFormatter>(
                "StringFormatter1", FTOptix.CoreBase.ObjectTypes.StringFormatter);
            // The .Format SETTER kills Studio from this thread (session-affine);
            // write the underlying HasParameter child. Arg order is (text, localeId).
            var fmtVar = InformationModel.MakeVariable(
                "Format", OpcUa.DataTypes.LocalizedText);
            fmtVar.Value = new UAValue(new LocalizedText(format, "en-US"));
            sf.Refs.AddReference(FTOptix.CoreBase.ReferenceTypes.HasParameter, fmtVar);

            var added = new StringBuilder();
            var srcHolders = new List<IUAVariable>();
            var srcTargets = new List<IUANode>();
            int i = 0;
            if (!string.IsNullOrEmpty(sources))
            {
                foreach (var sp in sources.Split(','))
                {
                    var s = sp.Trim();
                    if (s.Length == 0) continue;
                    var srcN = InformationModel.MakeVariable("Source" + i, OpcUa.DataTypes.BaseDataType);
                    if (raw)
                    {
                        // Literal NodePath: materialise a DynamicLink and set its value
                        // directly (resolves per instance at runtime), like bind raw=.
                        srcN.SetDynamicLink(null, DynamicLinkMode.Read);
                        var dl = srcN.Refs.GetVariable(FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                        if (dl == null) return ErrorJson("link_materialize_failed",
                            "could not materialise a DynamicLink on Source" + i);
                        dl.Value = s;
                    }
                    else
                    {
                        var srcVar = ResolveNode(s) as IUAVariable;
                        if (srcVar == null) return ErrorJson("source_not_variable",
                            "source is not a variable: " + s + " - pass raw=1 to attach an alias/template path literally");
                        srcN.SetDynamicLink(srcVar);
                        srcTargets.Add(srcVar);
                    }
                    sf.Refs.AddReference(FTOptix.CoreBase.ReferenceTypes.HasSource, srcN);
                    srcHolders.Add(srcN);
                    if (i > 0) added.Append(",");
                    added.Append("\"" + JsonEscape(s) + "\"");
                    i++;
                }
            }

            propVar.SetConverter(sf);
            sf.SetModellingRuleRecursive();
            // Re-point resolvable sources to their SAME-TYPE relative form, so an
            // instance reads its own value not the type's (field finding 2026-09-04).
            // Raw sources are already relative literals, so they are skipped.
            int relCount = 0;
            if (!raw)
            {
                int t = 0;
                for (int r = 0; r < srcHolders.Count; r++)
                {
                    if (t >= srcTargets.Count) break;
                    if (RelinkRelativeWithinType(srcHolders[r], srcTargets[t]) != null) relCount++;
                    t++;
                }
            }
            Log.Info("StudioBridge", "SFMT: OK \"" + format + "\" sources=" + i +
                     " raw=" + raw + " relative=" + relCount + " mode=" + mode +
                     " - SAVE, then render-verify");
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path) + "\",\"name\":\"" + JsonEscape(name) +
                   "\",\"format\":\"" + JsonEscape(format) + "\",\"sources\":[" + added +
                   "],\"placeholders\":" + (maxPlaceholder + 1) + ",\"bound\":" + i +
                   ",\"raw\":" + (raw ? "true" : "false") +
                   ",\"via\":\"string-formatter-converter\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // POST /bridge/node/attach-formatter?path=<node>&name=<prop>&format=<fmt>&sources=<n=path;..>[&mode=Read]
    // Attach a FORMATTED DYNAMIC LINK: a DynamicLink whose path is produced by a
    // StringFormatter from NAMED sources.
    //
    //   <prop>
    //     DynamicLink                  (NodePath)
    //       DynamicLinkFormatter       (StringFormatter)        <- HasConverter
    //         Format = "../NavPanel{#idx}@NodeId"               <- HasParameter
    //         Source<idx>              (BaseDataType)           <- HasSource
    //           DynamicLink -> <driver variable>
    //
    // In Studio this is ONE control: type {#token} into a dynamic link's path and
    // it becomes a "Formatted dynamic link" with a child link per token. The
    // StringFormatter is how it serializes, not a thing you assemble by hand.
    //
    // TWO THINGS MAKE OR BREAK THIS - both cost a long debugging session on
    // 2026-09-01, and both are invisible in describe_node:
    //
    //  1. NEVER touch StringFormatter.Format's PROPERTY SETTER from here. It
    //     takes Studio down outright - a corrupted-state exception .NET Core
    //     cannot catch, so the process vanishes with NOTHING logged. Write the
    //     underlying HasParameter child directly instead (below); same result,
    //     safe on this thread. Step-traced: MakeObject succeeded, the very next
    //     Format assignment killed it, detached AND attached alike.
    //  2. SetModellingRuleRecursive() is MANDATORY, not decoration. Without it
    //     every node serializes ModellingRule: None, and a None member is NOT
    //     instantiated into instances of its owning ObjectType. On a ScreenType
    //     that means the link exists at DESIGN time (looks perfect in Studio and
    //     in describe_node) and is simply absent at RUNTIME. That is the exact
    //     signature of "looks right, renders nothing".
    //
    // Everything else runs fine on the bridge's HTTP thread - no marshaling is
    // needed. DelayedTask(LogicObject) was tried and does NOT work here: a
    // design-time NetLogic's behaviour context is not running, so scheduled
    // work never fires.
    //
    // Converters no-op SILENTLY when mis-wired: ok:true means attached, never
    // correct. Render-verify - and check the owning type instantiates it.
    private string AttachFormatterInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string name = QueryParam(firstLine, "name");
        string format = QueryParam(firstLine, "format");
        string sources = QueryParam(firstLine, "sources");
        string modeStr = QueryParam(firstLine, "mode") ?? "Read";
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name) || string.IsNullOrEmpty(format))
            return ErrorJson("bad_query",
                "required: path, name, format (+ sources=name=path;name=path, mode=Read|ReadWrite|Write)");
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);

            IUAVariable propVar = node.GetVariable(name);
            if (propVar == null)
            {
                var gate = DeclaredPropertyGuard(node, name);
                if (gate != null) return gate;
                var arrGate = DeclaredArrayGuard(node, name);
                if (arrGate != null) return arrGate;
                if (node is IUAObject obj) propVar = obj.GetOrCreateVariable(name);
                else if (node is IUAObjectType objT) propVar = objT.GetOrCreateVariable(name);
            }
            if (propVar == null) return ErrorJson("property_not_found", "no property " + name + " on " + path);

            // Resolve EVERY source before scheduling, so a typo is reported
            // synchronously and leaves the model untouched.
            var srcNames = new List<string>();
            var srcVars = new List<IUAVariable>();
            if (!string.IsNullOrEmpty(sources))
            {
                foreach (var part in sources.Split(';'))
                {
                    var s = part.Trim();
                    if (s.Length == 0) continue;
                    int eq = s.IndexOf('=');
                    if (eq <= 0) return ErrorJson("bad_source", "want name=path, got: " + s);
                    string sn = s.Substring(0, eq).Trim();
                    string sp = s.Substring(eq + 1).Trim();
                    if (sn.Length == 0 || sp.Length == 0)
                        return ErrorJson("bad_source", "want name=path, got: " + s);
                    var sv = ResolveNode(sp) as IUAVariable;
                    if (sv == null) return ErrorJson("source_not_variable", "source is not a variable: " + sp);
                    srcNames.Add(sn);
                    srcVars.Add(sv);
                }
            }

            DynamicLinkMode mode;
            switch (modeStr)
            {
                case "Write": mode = DynamicLinkMode.Write; break;
                case "ReadWrite": mode = DynamicLinkMode.ReadWrite; break;
                default: mode = DynamicLinkMode.Read; break;
            }

            // DelayedTask(LogicObject) never fires at design time - a design-time
            // NetLogic's behaviour context is not running (its Start() does not
            // auto-fire either), so scheduled work just queues. Run inline.
            var err = BuildFormattedLink(propVar, format, srcNames, srcVars, mode);
            if (err != null) return ErrorJson("build_failed", err);

            var added = new StringBuilder();
            for (int i2 = 0; i2 < srcNames.Count; i2++)
            {
                if (i2 > 0) added.Append(",");
                added.Append("\"" + JsonEscape(srcNames[i2]) + "\"");
            }
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path + "/" + name) +
                   "\",\"format\":\"" + JsonEscape(format) + "\",\"sources\":[" + added +
                   "],\"mode\":\"" + JsonEscape(modeStr) +
                   "\",\"via\":\"formatted-dynamic-link\",\"applied\":true" +
                   ",\"note\":\"check the Output pane" +
                   " for FMT(task), then SAVE and render-verify\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // The NetLogic_CheatSheet "formatted dynamic link" recipe, followed exactly
    // except for the Format write (see 1. above - the setter is fatal here).
    //
    // DO NOT "simplify" these calls. Substituting SetConverter for
    // AddReference(HasConverter), SetDynamicLink(null) for ResetDynamicLink, or
    // dropping SetModellingRuleRecursive each produced a structure that read
    // correctly in describe_node and resolved to NOTHING at runtime. The
    // documented sequence is the contract; deviate only with a render test.
    private string BuildFormattedLink(IUAVariable targetVariable, string format,
                                    List<string> srcNames, List<IUAVariable> srcVars,
                                    DynamicLinkMode mode)
    {
        try
        {
            Log.Info("StudioBridge", "FMT: begin " + targetVariable.BrowseName);
            targetVariable.ResetDynamicLink();
            var newDynamicLink = InformationModel.MakeVariable<DynamicLink>(
                "DynamicLink", FTOptix.Core.DataTypes.NodePath);
            newDynamicLink.Value = "";
            var newStringFormatter = InformationModel.MakeObject<StringFormatter>(
                "DynamicLinkFormatter", FTOptix.CoreBase.ObjectTypes.StringFormatter);
            // The .Format SETTER kills Studio from this thread (step-traced).
            // Write the underlying HasParameter child instead - same result,
            // and safe here. Arg order is (text, localeId).
            var fmtVar = InformationModel.MakeVariable(
                "Format", OpcUa.DataTypes.LocalizedText);
            fmtVar.Value = new UAValue(new LocalizedText(format, ""));
            newStringFormatter.Refs.AddReference(
                FTOptix.CoreBase.ReferenceTypes.HasParameter, fmtVar);
            var srcHolders = new List<IUAVariable>();
            for (int i = 0; i < srcNames.Count; i++)
            {
                var src = InformationModel.MakeVariable(
                    "Source" + srcNames[i], OpcUa.DataTypes.BaseDataType);
                src.SetDynamicLink(srcVars[i]);
                newStringFormatter.Refs.AddReference(
                    FTOptix.CoreBase.ReferenceTypes.HasSource, src);
                srcHolders.Add(src);
            }
            newDynamicLink.Mode = mode;
            newDynamicLink.Refs.AddReference(
                FTOptix.CoreBase.ReferenceTypes.HasConverter, newStringFormatter);
            newStringFormatter.SetModellingRuleRecursive();
            targetVariable.Refs.AddReference(
                FTOptix.CoreBase.ReferenceTypes.HasDynamicLink, newDynamicLink);
            newDynamicLink.SetModellingRuleRecursive();
            // ONLY NOW is each Source variable in the tree (formatter -> link ->
            // target), so only now can its absolute link be re-pointed at the
            // relative form. Same reason as attach_expression: an absolute source
            // inside an ObjectType is read by every instance from the TYPE
            // (field finding 2026-09-04). Value writes only - no new nodes, so the
            // SetModellingRuleRecursive calls above still cover everything.
            int relCount = 0;
            for (int i = 0; i < srcHolders.Count; i++)
                if (RelinkRelativeWithinType(srcHolders[i], srcVars[i]) != null) relCount++;
            Log.Info("StudioBridge", "FMT: OK \"" + format + "\" sources=" +
                     srcNames.Count + " relative=" + relCount +
                     " mode=" + mode + " - SAVE, then render-verify");
            return null;
        }
        catch (Exception ex)
        {
            Log.Error("StudioBridge", "FMT FAILED: " + ExcMsg(ex));
            return ExcMsg(ex);
        }
    }

    // POST /bridge/node/reorder?path=X&position=front|back  (or &index=N) - change a
    // node's z-order among its siblings. In Optix render order = child order (a
    // HasOrderedComponent list): last child renders on TOP (front), first renders at
    // the BACK. This is Studio's "bring to front / send to back" (drag up/down), and
    // the enabler for a Panel background Rectangle behind existing children (the
    // panelbg gap).
    //
    // Index space is GRAPHIC CHILDREN: IUAVariable children (node properties) are
    // excluded from the count. index=0 means the first graphic object (rendered at
    // the back); index=N-1 means the last (rendered in front). Use GraphicChildren()
    // and GraphicIndexOf() to work in this space.
    private string ReorderInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string pos = QueryParam(firstLine, "position");
        string idxStr = QueryParam(firstLine, "index");
        if (string.IsNullOrEmpty(path))
            return ErrorJson("bad_query", "required: path, and position=front|back OR index=<int>");
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            var parent = node.Owner;
            if (parent == null) return ErrorJson("no_parent", "node has no parent: " + path);
            // Verify the node is reachable in its parent's Children (any kind).
            // This preserves the existing not_a_child guard before we narrow to graphic space.
            bool foundInChildren = false;
            foreach (var c in parent.Children) { if (c == node) { foundInChildren = true; break; } }
            if (!foundInChildren)
                return ErrorJson("not_a_child", "node is not in its parent's Children: " + path);
            // Work in graphic-children index space (IUAVariable children are excluded).
            var gkids = GraphicChildren(parent);
            int from = GraphicIndexOf(parent, node);
            if (from < 0)
                return ErrorJson("not_a_graphic_child",
                    "node is not a graphic child of its parent (it may be an IUAVariable/property): " + path);
            int target;
            if (pos == "front") target = gkids.Count - 1;   // last graphic child = rendered in FRONT
            else if (pos == "back") target = 0;              // first graphic child = rendered BEHIND
            else if (!int.TryParse(idxStr, out target))
                return ErrorJson("bad_query", "need position=front|back or index=<int>");
            // Clamp target into valid graphic-children range.
            if (target < 0) target = 0;
            if (target > gkids.Count - 1) target = gkids.Count - 1;
            // Noop short-circuit.
            if (target == from)
                return "{\"ok\":true,\"path\":\"" + JsonEscape(path) +
                       "\",\"from\":" + from + ",\"requested\":" + target +
                       ",\"achieved\":" + from + ",\"space\":\"graphic_children\"" +
                       ",\"moves\":0,\"noop\":true}";
            // Non-destructive in-place reorder via MoveUp()/MoveDown() (Sort-project-nodes:
            // the sanctioned API). MoveUp -> earlier in the child list = toward the BACK;
            // MoveDown -> later = toward the FRONT. NOTE: only effective on graphic objects
            // that live inside a TYPE (ScreenType/PanelType) - a plain instance's children
            // won't move. Reload the runtime page to see the visual effect.
            //
            // Re-reading loop: re-read GraphicIndexOf after every move so we track actual
            // model state rather than counting assumed steps. Bounded by 2*count+4 to
            // tolerate a stuck model; the no-progress check exits immediately if MoveUp/
            // MoveDown fails to shift the node (already at a boundary).
            int count = gkids.Count;
            int guard = 2 * count + 4;
            int moves = 0;
            for (int step = 0; step < guard; step++)
            {
                int cur = GraphicIndexOf(parent, node);
                if (cur == target) break;           // reached target -- done
                int prev = cur;
                if (cur > target) node.MoveUp();
                else               node.MoveDown();
                moves++;
                if (GraphicIndexOf(parent, node) == prev) break;  // no-progress: boundary
            }
            int achieved = GraphicIndexOf(parent, node);
            if (achieved != target)
                return "{\"ok\":false,\"error\":\"reorder_index_unreached\"" +
                       ",\"requested\":" + target + ",\"achieved\":" + achieved +
                       ",\"from\":" + from + ",\"space\":\"graphic_children\"" +
                       ",\"moves\":" + moves + "}";
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path) +
                   "\",\"from\":" + from + ",\"requested\":" + target +
                   ",\"achieved\":" + achieved + ",\"space\":\"graphic_children\"" +
                   ",\"moves\":" + moves + ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    /// <summary>
    /// Returns the graphic-object children of <paramref name="parent"/>, filtering
    /// out IUAVariable children (which are node properties, not visual elements).
    /// This is the index space used by ReorderInline.
    /// </summary>
    private static List<IUANode> GraphicChildren(IUANode parent)
    {
        var result = new List<IUANode>();
        foreach (var child in parent.Children)
        {
            if (!(child is IUAVariable))
                result.Add(child);
        }
        return result;
    }

    /// <summary>
    /// Returns the zero-based index of <paramref name="node"/> within the graphic
    /// children of <paramref name="parent"/> (IUAVariable children excluded), or
    /// -1 if <paramref name="node"/> is not a graphic child.
    /// </summary>
    private static int GraphicIndexOf(IUANode parent, IUANode node)
    {
        int idx = 0;
        foreach (var child in parent.Children)
        {
            if (child is IUAVariable) continue;
            if (child == node) return idx;
            idx++;
        }
        return -1;
    }

    // GET /bridge/node/typeinfo?path=X - diagnose a node's KIND. Is it an ObjectType
    // (a reusable *type*, like a right-click "Add Screen" -> a subtype of Screen) or an
    // Object (an *instance*, like a bridge MakeObject of the Screen type)? For a type,
    // walk the SuperType chain so we can SEE the inheritance (e.g. Screen1 -> Screen ->
    // ... vs a bare instance whose type is the base Screen). This is the diagnostic for
    // "bridge screens render in the designer but not at runtime".
    private string TypeInfoJson(string path)
    {
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            var sb = new StringBuilder();
            sb.Append("{\"path\":\"" + JsonEscape(path) +
                      "\",\"browse_name\":\"" + JsonEscape(node.BrowseName) +
                      "\",\"node_class\":\"" + node.NodeClass +
                      "\",\"dotnet_type\":\"" + JsonEscape(node.GetType().Name) + "\"");
            if (node is IUAObjectType ot)
            {
                sb.Append(",\"is_type\":true,\"supertype_chain\":[");
                int i = 0;
                for (var cur = ot.SuperType; cur != null && i < 20; cur = cur.SuperType)
                {
                    if (i++ > 0) sb.Append(",");
                    sb.Append("{\"browse_name\":\"" + JsonEscape(cur.BrowseName) +
                              "\",\"dotnet_type\":\"" + JsonEscape(cur.GetType().Name) + "\"}");
                }
                sb.Append("]");
            }
            else
            {
                sb.Append(",\"is_type\":false");
            }
            sb.Append("}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // Resolve a widget type name (e.g. "Panel") to its generated Optix CLR proxy
    // Type by scanning loaded assemblies for an FTOptix-namespaced type of that
    // name. Read-only reflection - no instance, no typed setter (which crashes
    // off-thread), just metadata. Null if not found (caller falls back).
    private Type ResolveWidgetClrType(string typeName)
    {
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            Type[] types;
            try { types = asm.GetTypes(); }
            catch { continue; }  // ReflectionTypeLoadException on a partial assembly
            foreach (var ty in types)
                if (ty.Name == typeName && ty.Namespace != null
                    && ty.Namespace.StartsWith("FTOptix"))
                    return ty;
        }
        return null;
    }

    // Inheritance-aware list of a live node's settable property names, filtered to
    // FTOptix-declared props (drops UAManagedCore infra like NodeId/BrowseName).
    // The valid set the validity gate hands back on an unknown-property rejection,
    // and the candidate pool the did-you-mean suggestion matches against.
    private System.Collections.Generic.List<string> DeclaredPropertyNames(object node)
    {
        return node.GetType()
                   .GetProperties(BindingFlags.Public | BindingFlags.Instance)
                   .Where(IsLegendProp)
                   .Select(pi => pi.Name).Distinct().OrderBy(x => x).ToList();
    }

    // JSON-array-fragment form of DeclaredPropertyNames, capped at MaxItems, for
    // embedding in an unknown_property error's valid_properties list.
    private string PropertyNamesJsonList(object node)
    {
        var sb = new StringBuilder();
        int count = 0;
        foreach (var pn in DeclaredPropertyNames(node))
        {
            if (count >= MaxItems) break;
            if (count++ > 0) sb.Append(",");
            sb.Append("\"" + JsonEscape(pn) + "\"");
        }
        return sb.ToString();
    }

    // Guard before GetOrCreateVariable at any CALLER-supplied property name.
    // GetOrCreateVariable fabricates an orphan variable for a property the type does
    // not declare (e.g. Panel.BorderThickness), and the renderer then AVs on it
    // off-thread and kills Studio (0xC0000005). Returns null when it is
    // safe to materialize (already-materialized OR type-declared, inheritance-aware
    // via the generated Optix proxy), else an unknown_property error JSON carrying
    // the valid set. Every user-facing materialization site MUST call this first.
    private string DeclaredPropertyGuard(IUANode node, string name)
    {
        if (node.GetVariable(name) != null) return null;   // already materialized
        if (IsNodeAttributeName(name))
            return "{\"error\":{\"code\":\"node_attribute_not_settable\",\"message\":\"" +
                   JsonEscape(name + " is a node attribute, not a UA child property - " +
                       "materializing it as a variable crashes Studio. DisplayName is " +
                       "settable ONLY via set_property (dedicated attribute route); to " +
                       "rename a node use the rename op (or move with new_name).") + "\"}}";
        // The acceptance test requires the CLR match to be DECLARED IN AN FTOptix
        // NAMESPACE. A bare any-public-property match false-accepted UAManagedCore
        // node ATTRIBUTES (DisplayName, BrowseName, Description, NodeId, ...): they
        // exist as CLR properties on every node proxy but are not UA child variables,
        // so GetOrCreateVariable fabricated an orphan and Studio died on the next
        // render (crash confirmed live 2026-08-16, agent set DisplayName).
        if (node.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
                .Any(p => p.Name == name && p.DeclaringType != null
                       && p.DeclaringType.Namespace != null
                       && p.DeclaringType.Namespace.StartsWith("FTOptix")))
        {
            // The proxy can declare a property the loaded UA type does not:
            // SpinBox.ValueChangeBehaviour (Studio 1.7.4.32) passes the CLR check,
            // then GetOrCreateVariable AccessViolates inside Studio's own
            // OptionalInstanceDeclarationExtensions - with any value, valid or not
            // (1.0.8 battle test, twice; WER: coreclr c0000005 at
            // GetOrCreateVariable <- SetPropertyInline). Materialize only what an
            // ObjectType in the instance's UA supertype chain actually declares.
            if (!(node is UAObject inst) || inst.ObjectType == null) return null;
            IUAObjectType cur = inst.ObjectType;
            for (int i = 0; cur != null && i < 20; i++, cur = cur.SuperType)
                if (cur.GetVariable(name) != null) return null;
            return "{\"error\":{\"code\":\"proxy_only_property\",\"message\":\"" +
                   JsonEscape(node.GetType().Name + "." + name + " is declared by the .NET " +
                       "proxy but by no ObjectType in this node's UA type chain; creating it " +
                       "crashes Studio. Set it in Studio, or leave it at its default.") + "\"}}";
        }
        // Mirror the wire_event reject-with-valid-list: hand back the authoritative
        // set + a best-effort suggestion, baked into the message so it survives the
        // Python-side message/code flattening (a sibling did_you_mean field alone is
        // dropped there). The property_not_found sites downstream are only reached
        // AFTER this returns null (name already matched a real property), so they
        // deliberately carry no suggestion - the name is not a typo there.
        var valid = DeclaredPropertyNames(node);
        var suggestion = SuggestPropertyName(name, valid);
        var sb = new StringBuilder();
        sb.Append("{\"error\":{\"code\":\"unknown_property\",\"message\":\"");
        sb.Append(JsonEscape(node.GetType().Name + " has no settable property '" + name + "'" +
            (suggestion != null ? " (did you mean " + suggestion + "?)" : "") +
            " (call describe_type/describe_node for the valid set)"));
        sb.Append("\"");
        if (suggestion != null)
        {
            sb.Append(",\"did_you_mean\":\""); sb.Append(JsonEscape(suggestion)); sb.Append("\"");
        }
        sb.Append(",\"valid_properties\":[" + PropertyNamesJsonList(node) + "]}}");
        return sb.ToString();
    }

    // UA node ATTRIBUTES the proxy exposes as CLR properties. Not UA child
    // variables - materializing one crashes Studio (see DeclaredPropertyGuard).
    // Named explicitly so the rename-intent names get the targeted nudge above
    // instead of falling through to unknown_property.
    private static bool IsNodeAttributeName(string name)
    {
        switch (name)
        {
            case "DisplayName":
            case "BrowseName":
            case "Description":
            case "NodeId":
            case "NodeClass":
                return true;
            default:
                return false;
        }
    }

    // ---- live-model write endpoints (inline mutation from the HTTP thread) ----
    //
    // POST /bridge/model/variable?name=X&parent=Model&datatype=Boolean
    // Creates a variable via InformationModel.MakeVariable + parent.Add, INLINE
    // on this background socket thread. The result reports ok/error so we learn
    // whether off-thread design-time mutation is safe (success) or needs
    // main-thread marshaling (exception / instability). Params via query string
    // to avoid body parsing.
    private string WriteVariableInline(string firstLine)
    {
        string name = QueryParam(firstLine, "name");
        string parent = QueryParam(firstLine, "parent") ?? "Model";
        string dtName = QueryParam(firstLine, "datatype") ?? "Boolean";
        if (string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "missing required query param: name");
        try
        {
            var parentNode = ResolveNode(parent);
            if (parentNode == null)
                return ErrorJson("node_not_found", "no parent node at: " + parent);
            var v = InformationModel.MakeVariable(name, ResolveDataType(dtName));
            parentNode.Add(v);
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(parent + "/" + name) +
                   "\",\"datatype\":\"" + JsonEscape(dtName) +
                   "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"mode\":\"inline\",\"error\":\"" +
                   JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // POST /bridge/node/retype?path=<node>&datatype=String[&dims=10|scalar]
    // Change a VARIABLE's DataType in place - optionally its array shape too -
    // while KEEPING the node: its NodeId, its children (DynamicLink, converters,
    // Mode) and every inbound reference survive, which delete+create cannot
    // offer (and create_variable cannot make an array at all).
    //
    // The current value is converted ELEMENT-WISE so design-time defaults carry
    // over, and the float->text arm goes through ToString("R"): 22.3f becomes
    // "22.3", never the widened 22.299999237060547. Text->number parses
    // invariant; anything unparsable lands on the type's default rather than
    // failing the whole op.
    //
    // Why it exists: a common fix for Float->text precision noise on recipe
    // values is to STORE them as String and let the PLC driver cast String<->REAL
    // at the link. That means retyping the model arrays plus every StoreColumn
    // behind them, a change Studio's property grid makes one node at a time and
    // the bridge could not express at all.
    //
    // dims: omitted keeps the current shape; "scalar" (or "0") collapses to a
    // scalar; "N" (or "N,M") sets the array dimensions.
    private string RetypeVariableInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string dtName = QueryParam(firstLine, "datatype");
        string dimsRaw = QueryParam(firstLine, "dims");
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(dtName))
            return ErrorJson("bad_query", "required query params: path, datatype");
        if (RetypeClrType(dtName) == null)
            return ErrorJson("bad_datatype", "unsupported datatype '" + dtName +
                "' - one of Boolean, Int16, Int32, Int64, UInt16, UInt32, UInt64, " +
                "Float, Double, String, DateTime");
        try
        {
            var node = ResolveNodeLoose(path);
            if (node == null)
                return ErrorJson("node_not_found", "no node at: " + path);
            var v = node as IUAVariable;
            if (v == null)
                return ErrorJson("not_a_variable", "'" + path + "' is a " +
                    node.GetType().Name + ", not a variable - only variables have a DataType");

            uint[] oldDims = null;
            try { oldDims = v.ArrayDimensions; } catch { }
            bool wasArray = oldDims != null && oldDims.Length > 0;
            uint[] newDims = oldDims;
            if (!string.IsNullOrEmpty(dimsRaw))
            {
                if (dimsRaw == "scalar" || dimsRaw == "0") newDims = null;
                else
                {
                    var parts = dimsRaw.Split(',');
                    newDims = new uint[parts.Length];
                    for (int i = 0; i < parts.Length; i++)
                    {
                        uint d;
                        if (!uint.TryParse(parts[i].Trim(), out d) || d == 0)
                            return ErrorJson("bad_dims", "dims must be 'scalar' or positive integers, got '" + dimsRaw + "'");
                        newDims[i] = d;
                    }
                }
            }
            bool toArray = newDims != null && newDims.Length > 0;
            if (toArray && newDims.Length != 1)
                return ErrorJson("bad_dims", "only one-dimensional arrays are supported (got " + newDims.Length + " dims)");

            string oldDt = "?";
            try { oldDt = v.Context.GetDataType(v.DataType).BrowseName; } catch { }

            object oldVal = null;
            try { var uv = v.Value; oldVal = uv == null ? null : uv.Value; } catch { }

            Type clr = RetypeClrType(dtName);
            object newVal;
            int converted = 0;
            if (toArray)
            {
                int n = (int)newDims[0];
                var arr = Array.CreateInstance(clr, n);
                var src = oldVal as Array;
                for (int i = 0; i < n; i++)
                {
                    object e = (src != null && i < src.Length) ? src.GetValue(i) : null;
                    if (e != null) converted++;
                    arr.SetValue(RetypeScalar(e, dtName, clr), i);
                }
                newVal = arr;
            }
            else
            {
                object e = oldVal;
                var src = oldVal as Array;
                if (src != null) e = src.Length > 0 ? src.GetValue(0) : null;
                if (e != null) converted++;
                newVal = RetypeScalar(e, dtName, clr);
            }

            // Shape first, then type, then value - the value assignment is what
            // Studio checks against the declared shape/type.
            v.ArrayDimensions = toArray ? newDims : new uint[0];
            v.ValueRank = toArray ? ValueRank.OneDimension : ValueRank.Scalar;
            v.DataType = ResolveDataType(dtName);
            v.Value = new UAValue(newVal);

            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"path\":\"").Append(JsonEscape(path)).Append("\"");
            sb.Append(",\"from\":{\"datatype\":\"").Append(JsonEscape(oldDt)).Append("\",\"dims\":");
            sb.Append(wasArray ? "[" + string.Join(",", oldDims) + "]" : "null").Append("}");
            sb.Append(",\"to\":{\"datatype\":\"").Append(JsonEscape(dtName)).Append("\",\"dims\":");
            sb.Append(toArray ? "[" + string.Join(",", newDims) + "]" : "null").Append("}");
            sb.Append(",\"values_converted\":").Append(converted);
            sb.Append(",\"sample\":[");
            var sample = newVal as Array;
            int shown = 0;
            if (sample != null)
            {
                for (int i = 0; i < sample.Length && i < 3; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append("\"").Append(JsonEscape(Convert.ToString(sample.GetValue(i), CultureInfo.InvariantCulture))).Append("\"");
                    shown++;
                }
            }
            else
                sb.Append("\"").Append(JsonEscape(Convert.ToString(newVal, CultureInfo.InvariantCulture))).Append("\"");
            sb.Append("],\"mode\":\"inline\",\"thread\":\"http-bg\"");
            sb.Append(",\"nudge\":\"DataType changed on the LIVE node; children and inbound links kept. ");
            sb.Append("A StoreColumn retype changes what the table is CREATED with - an existing ");
            sb.Append("database file keeps its old column affinity until the table is recreated. ");
            sb.Append("Save (optix_save) and restart the emulator to see it.\"}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"mode\":\"inline\",\"error\":\"" +
                   JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    private static Type RetypeClrType(string name)
    {
        switch (name)
        {
            case "Boolean": return typeof(bool);
            case "Int16": return typeof(short);
            case "Int32": return typeof(int);
            case "Int64": return typeof(long);
            case "UInt16": return typeof(ushort);
            case "UInt32": return typeof(uint);
            case "UInt64": return typeof(ulong);
            case "Float": return typeof(float);
            case "Double": return typeof(double);
            case "String": return typeof(string);
            case "DateTime": return typeof(DateTime);
            default: return null;
        }
    }

    // One element, old type -> new type. Float/double to text go through "R"
    // (shortest round-trip) so the text is what a human typed, never the
    // widened double. Text to number parses invariant; junk -> default.
    private static object RetypeScalar(object e, string dtName, Type clr)
    {
        var inv = CultureInfo.InvariantCulture;
        if (e is UAValue uav) e = uav.Value;
        if (dtName == "String")
        {
            if (e == null) return "";
            if (e is float f) return float.IsNaN(f) || float.IsInfinity(f) ? "0" : f.ToString("R", inv);
            if (e is double d) return double.IsNaN(d) || double.IsInfinity(d) ? "0" : d.ToString("R", inv);
            if (e is bool b) return b ? "true" : "false";
            if (e is DateTime dt) return dt.ToString("o", inv);
            return Convert.ToString(e, inv) ?? "";
        }
        if (e == null) return clr == typeof(DateTime) ? (object)DateTime.MinValue : Activator.CreateInstance(clr);
        try
        {
            if (e is string s)
            {
                s = s.Trim();
                if (dtName == "Boolean")
                    return s == "1" || string.Equals(s, "true", StringComparison.OrdinalIgnoreCase);
                if (dtName == "DateTime")
                {
                    DateTime parsed;
                    return DateTime.TryParse(s, inv, System.Globalization.DateTimeStyles.RoundtripKind, out parsed) ? parsed : DateTime.MinValue;
                }
                // Integers typed as "22.0" should still land: parse as double, then narrow.
                double dv;
                if (!double.TryParse(s, System.Globalization.NumberStyles.Float, inv, out dv))
                    return Activator.CreateInstance(clr);
                if (clr == typeof(float)) return (float)dv;
                if (clr == typeof(double)) return dv;
                return Convert.ChangeType(Math.Round(dv), clr, inv);
            }
            if (dtName == "Boolean") return Convert.ToBoolean(e, inv);
            if (dtName == "DateTime") return e is DateTime ? e : DateTime.MinValue;
            return Convert.ChangeType(e, clr, inv);
        }
        catch
        {
            return clr == typeof(DateTime) ? (object)DateTime.MinValue : Activator.CreateInstance(clr);
        }
    }

    private static NodeId ResolveDataType(string name)
    {
        switch (name)
        {
            case "Boolean": return OpcUa.DataTypes.Boolean;
            case "Byte": return OpcUa.DataTypes.Byte;
            case "SByte": return OpcUa.DataTypes.SByte;
            case "Int16": return OpcUa.DataTypes.Int16;
            case "Int32": return OpcUa.DataTypes.Int32;
            case "Int64": return OpcUa.DataTypes.Int64;
            case "UInt16": return OpcUa.DataTypes.UInt16;
            case "UInt32": return OpcUa.DataTypes.UInt32;
            case "UInt64": return OpcUa.DataTypes.UInt64;
            case "Float": return OpcUa.DataTypes.Float;
            case "Double": return OpcUa.DataTypes.Double;
            case "String": return OpcUa.DataTypes.String;
            case "NodeId": return OpcUa.DataTypes.NodeId;   // was silently Boolean
            case "DateTime": return OpcUa.DataTypes.DateTime;
            case "LocalizedText": return OpcUa.DataTypes.LocalizedText;
            case "Range": return OpcUa.DataTypes.Range;
            case "Color": return FTOptix.Core.DataTypes.Color;
            case "ResourceUri": return FTOptix.Core.DataTypes.ResourceUri;
            default:
                // Unrecognized datatype names USED TO silently fall through to
                // Boolean here - a caller asking for "Color" (or any typo or
                // unsupported name) got a Boolean variable back with no error
                // and no warning that the requested type was not honored
                // (found via live probing 2026-08-31: a create_variable batch
                // validate for datatype="Color", and even a nonsense string,
                // both reported clean with zero errors). Fail loud instead -
                // list exactly what IS supported so the caller can fix the
                // request, rather than silently handing back the wrong type.
                throw new ArgumentException(
                    "unsupported datatype '" + name + "' - expected one of: " +
                    "Boolean, Byte, SByte, Int16, Int32, Int64, UInt16, UInt32, " +
                    "UInt64, Float, Double, String, DateTime, NodeId, " +
                    "LocalizedText, Range, Color, ResourceUri");
        }
    }

    // Shared duplicate-sibling refusal for the structural-authoring family.
    // Optix happily creates same-name siblings which are then unaddressable
    // by path; Studio's UI auto-suffixes, the bridge refuses loud.
    private static string DupNameGuard(IUANode parent, string name, string parentPath)
    {
        foreach (var existing in parent.Children)
            if (existing.BrowseName == name)
                return ErrorJson("name_exists",
                    "a node named '" + name + "' already exists under '" +
                    parentPath + "' - pick a different name or delete it first");
        return null;
    }

    // POST /bridge/model/folder?parent=<path>&name=<n>
    // Structural Folder (OpcUa FolderType) - organizational node, not a UI
    // control, so it lives outside the create_widget catalog by design.
    private string CreateFolderInline(string firstLine)
    {
        string parent = QueryParam(firstLine, "parent");
        string name = QueryParam(firstLine, "name");
        if (string.IsNullOrEmpty(parent) || string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "required query params: parent, name");
        try
        {
            var parentNode = ResolveNode(parent);
            if (parentNode == null)
                return ErrorJson("node_not_found", "no parent node at: " + parent);
            var dup = DupNameGuard(parentNode, name, parent);
            if (dup != null) return dup;
            var f = InformationModel.MakeObject(name, OpcUa.ObjectTypes.FolderType);
            parentNode.Add(f);
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(parent + "/" + name) +
                   "\",\"kind\":\"folder\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    // POST /bridge/model/object?parent=<path>&name=<n>[&type=<type path>]
    // Plain structural Object (BaseObjectType) container, OR an INSTANCE of a
    // project-defined ObjectType when `type` is given (the reuse half of the
    // create_type/templates workflow): MakeObject(name, customType.NodeId) per
    // the NetLogic cheatsheet's "DesignTime creation of custom Object instances".
    private string CreateObjectInline(string firstLine)
    {
        string parent = QueryParam(firstLine, "parent");
        string name = QueryParam(firstLine, "name");
        string typePath = QueryParam(firstLine, "type");
        if (string.IsNullOrEmpty(parent) || string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "required query params: parent, name (+ optional type=<ObjectType path>)");
        try
        {
            var parentNode = ResolveNode(parent);
            if (parentNode == null)
                return ErrorJson("node_not_found", "no parent node at: " + parent);
            var dup = DupNameGuard(parentNode, name, parent);
            if (dup != null) return dup;
            // GridLayoutProperties must land in the FTOptix.UI namespace; a generic
            // MakeObject would qualify the browse name with the project namespace and
            // the engine silently ignores RowStart/ColumnStart on it (bridge >= 1.0.8).
            if (name == "GridLayoutProperties")
                return TryCreateUiNamespaceChild(parentNode, name, parent);
            NodeId typeId = OpcUa.ObjectTypes.BaseObjectType;
            string typeLabel = "BaseObjectType";
            if (!string.IsNullOrEmpty(typePath))
            {
                IUANode typeNode = null;
                // A bare or Module.Type name is a BUILTIN catalog type first
                // (DigitalAlarm, DataLogger, User, ...; see ResolveUiTypeId
                // step (a2)); anything with a slash is a project type path.
                if (typePath.IndexOf('/') < 0)
                {
                    bool _viaBrowseNameObj;
                    var builtinId = ResolveUiTypeId(typePath, out _viaBrowseNameObj);
                    if (builtinId != null)
                    {
                        try { typeNode = InformationModel.Get(builtinId); } catch { typeNode = null; }
                    }
                }
                if (typeNode == null) typeNode = ResolveNode(typePath);
                if (typeNode == null)
                    return ErrorJson("type_not_found", "no node at type path: " + typePath +
                        " (not a builtin catalog type either - pass Module.Type, e.g. FTOptix.Alarm.DigitalAlarm)");
                if (typeNode.NodeClass != NodeClass.ObjectType)
                    return ErrorJson("not_a_type",
                        typePath + " is " + typeNode.NodeClass +
                        ", not an ObjectType - pass the TYPE node (e.g. UI/Templates/MyCard)," +
                        " not an instance; create one with /bridge/model/type first");
                typeId = typeNode.NodeId;
                typeLabel = typePath;
            }
            var o = InformationModel.MakeObject(name, typeId);
            parentNode.Add(o);
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(parent + "/" + name) +
                   "\",\"type\":\"" + JsonEscape(typeLabel) +
                   "\",\"node_class\":\"" + o.NodeClass +
                   "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    // POST /bridge/model/netlogic?parent=<path>&name=<ClassName>
    // Create a NetLogicObject node bound to a C# class BY NAME. This is the piece
    // that used to force a manual Studio "New -> NetLogic" step: it turns out a
    // NetLogic node carries no proxy and no code-reference property - Optix binds
    // the node to a class whose name equals the node BrowseName, and the SDK-style
    // NetSolution .csproj auto-globs every .cs, so a matching class compiles in on
    // the next build. Proven by a name-bound probe: a hand-authored NetLogic ran
    // Start() at runtime with no Studio menu step.
    // Caller contract: the class named `name` must exist in the NetSolution and
    // match exactly; add the .cs (design-time authoring), then rebuild/run so the
    // runtime instantiates it. Placement matters for runtime NetLogics that use
    // Owner (put it under the object whose siblings it reads).
    private string CreateNetLogicInline(string firstLine)
    {
        string parent = QueryParam(firstLine, "parent");
        string name = QueryParam(firstLine, "name");
        if (string.IsNullOrEmpty(parent) || string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "required query params: parent, name (name must equal the C# class name)");
        try
        {
            var parentNode = ResolveNode(parent);
            if (parentNode == null)
                return ErrorJson("node_not_found", "no parent node at: " + parent);
            var dup = DupNameGuard(parentNode, name, parent);
            if (dup != null) return dup;
            var o = InformationModel.MakeObject(name, FTOptix.NetLogic.ObjectTypes.NetLogic);
            parentNode.Add(o);
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(parent + "/" + name) +
                   "\",\"type\":\"NetLogic\",\"node_class\":\"" + o.NodeClass +
                   "\",\"bound_class\":\"" + JsonEscape(name) +
                   "\",\"note\":\"class must exist in NetSolution and match name exactly; rebuild/run to instantiate\"" +
                   ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    // POST /bridge/model/type?name=<n>&parent=<path>[&base=<catalog name | type path>]
    // Creates an ObjectType (a reusable template). base resolves against the
    // builtin UI catalog first (RowLayout, Button, ...), then as a project path
    // to another ObjectType (subtyping a custom type); empty = bare
    // BaseObjectType-derived (model-side structured types, cheatsheet
    // "NewMotorType"). Children are then authored INTO the type with the
    // normal tools - proven by MainWindow (a WindowType) taking children today.
    // NOTE: no promote-by-location magic - Studio auto-types a widget dropped
    // at the Templates root; the bridge only ever does what the call says.
    private string CreateTypeInline(string firstLine)
    {
        string name = QueryParam(firstLine, "name");
        string parent = QueryParam(firstLine, "parent");
        string baseName = QueryParam(firstLine, "base");
        if (string.IsNullOrEmpty(name) || string.IsNullOrEmpty(parent))
            return ErrorJson("bad_query", "required query params: name, parent (+ optional base=<catalog type or type path>)");
        try
        {
            var parentNode = ResolveNode(parent);
            if (parentNode == null)
                return ErrorJson("node_not_found",
                    "no parent node at: " + parent + " - create it first (/bridge/model/folder)");
            var dup = DupNameGuard(parentNode, name, parent);
            if (dup != null) return dup;
            IUANode newType;
            string baseLabel;
            if (string.IsNullOrEmpty(baseName))
            {
                newType = InformationModel.MakeObjectType(name);
                baseLabel = "BaseObjectType";
            }
            else
            {
                bool _viaBrowseNameBase;
                var baseId = ResolveUiTypeId(baseName, out _viaBrowseNameBase);
                if (baseId == null)
                {
                    var baseNode = ResolveNode(baseName);
                    if (baseNode != null && baseNode.NodeClass == NodeClass.ObjectType)
                        baseId = baseNode.NodeId;
                }
                if (baseId == null)
                    return ErrorJson("type_not_found",
                        "base '" + baseName + "' is neither a builtin UI type " +
                        "(optix_list_ui_types) nor a path to a project ObjectType");
                newType = InformationModel.MakeObjectType(name, baseId);
                baseLabel = baseName;
            }
            parentNode.Add(newType);
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(parent + "/" + name) +
                   "\",\"base\":\"" + JsonEscape(baseLabel) +
                   "\",\"node_class\":\"" + newType.NodeClass +
                   "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    // POST /bridge/node/convert-to-type?path=<instance>&type_name=<n>&types_folder=<path>[&replace=true]
    // Studio's right-click "Convert to Type" has no public API (cheatsheet lists
    // it as UI-only), so this reproduces it by RE-AUTHORING: new ObjectType
    // subtyping the instance's own type, then a recursive COPY of the subtree
    // into it (fresh MakeObject/MakeVariable born in the type + raw value copy
    // + re-created DynamicLinks). NEVER move the live children: re-parenting
    // instance children into a type (Children.Remove + type.Add) left the model
    // in a state whose traversal ACCESS-VIOLATED Studio (confirmed live
    // 2026-07-17 - the describe after a move-based convert killed the process),
    // and the instantiate step silently hollowed the type. Born-in-type
    // authoring is the mechanism the plan-ahead create_type path proved safe
    // and propagation-correct. Constructs the copy can't reproduce (converters,
    // exotic child classes) are SKIPPED and listed in the response - honest
    // partial coverage instead of a half-copied template.
    private string ConvertToTypeInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string typeName = QueryParam(firstLine, "type_name");
        string typesFolder = QueryParam(firstLine, "types_folder");
        bool replace = (QueryParam(firstLine, "replace") ?? "true") != "false";
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(typeName) || string.IsNullOrEmpty(typesFolder))
            return ErrorJson("bad_query", "required query params: path, type_name, types_folder (+ optional replace=false)");
        var steps = new StringBuilder();
        try
        {
            var node = ResolveNode(path);
            if (node == null)
                return ErrorJson("node_not_found", "no node at: " + path);
            if (node.NodeClass == NodeClass.ObjectType)
                return ErrorJson("already_a_type", path + " is already an ObjectType");
            if (!(node is IUAObject))
                return ErrorJson("not_an_object", path + " is " + node.NodeClass + ", not an Object instance");
            var folderNode = ResolveNode(typesFolder);
            if (folderNode == null)
                return ErrorJson("folder_not_found",
                    "no types folder at: " + typesFolder + " - create it first (/bridge/model/folder)");
            var dup = DupNameGuard(folderNode, typeName, typesFolder);
            if (dup != null) return dup;

            // Supertype = the instance's own ObjectType (RowLayout, ...) so the
            // new type keeps its rendering/behavior, mirroring Studio's refactor.
            NodeId superId = null;
            var uo = node as UAObject;
            if (uo != null && uo.ObjectType != null) superId = uo.ObjectType.NodeId;
            var newType = superId != null
                ? InformationModel.MakeObjectType(typeName, superId)
                : InformationModel.MakeObjectType(typeName);
            folderNode.Add(newType);
            steps.Append("\"create_type\"");

            int copied = 0;
            var skipped = new StringBuilder();
            var fixups = new List<LinkFixup>();
            string cerr = CopySubtreeInto(node, newType, ref copied, skipped, fixups, 0);
            if (cerr != null)
            {
                // Copy failed part-way: remove the half-built type (a fresh,
                // never-instantiated ObjectType - safe to delete) and report.
                newType.Delete();
                return ErrorJson("copy_failed", cerr + " - nothing was changed (half-built type removed)");
            }
            ApplyLinkFixups(node, newType, fixups, skipped);
            steps.Append(",\"copy_subtree\"");

            string instancePath = null;
            if (replace)
            {
                var owner = node.Owner;
                var name = node.BrowseName;
                var ownerPath = NodePathOf(owner);
                node.Delete();
                steps.Append(",\"delete_original\"");
                var inst = InformationModel.MakeObject(name, newType.NodeId);
                owner.Add(inst);
                steps.Append(",\"instantiate\"");
                instancePath = string.IsNullOrEmpty(ownerPath) ? name : ownerPath + "/" + name;
            }

            // Link audit on the copied subtree: recreated links resolve by
            // construction, but verify and report anyway (trust nothing).
            int absOk = 0, relUnverified = 0;
            var broken = new StringBuilder();
            AuditLinks(newType, ref absOk, ref relUnverified, broken);

            return "{\"ok\":true,\"type_path\":\"" + JsonEscape(typesFolder + "/" + typeName) +
                   "\",\"copied_nodes\":" + copied +
                   ",\"skipped\":[" + skipped + "]" +
                   ",\"replaced\":" + (replace ? "true" : "false") +
                   (instancePath != null ? ",\"instance_path\":\"" + JsonEscape(instancePath) + "\"" : "") +
                   ",\"links_verified\":" + absOk +
                   ",\"relative_links_unverified\":" + relUnverified +
                   ",\"broken_links\":[" + broken + "]" +
                   ",\"steps\":[" + steps + "],\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"steps\":[" + steps + "],\"error\":\"" +
                   JsonEscape(ExcMsg(ex)) +
                   "\",\"nudge\":\"conversion stopped mid-way - inspect " + JsonEscape(path) +
                   " and the types folder with describe_node before retrying\"}";
        }
    }

    // POST /bridge/node/move?path=<node>&new_parent=<path>[&new_name=<n>]
    // Reparent a live instance. NEVER a node-model Remove+Add - re-parenting
    // live children corrupted the model and crashed Studio (crash class #3,
    // 2026-07-17). Instead: RE-AUTHOR a copy under the new parent (same
    // machinery as convert_to_type, proven safe), apply link fixups, delete
    // the original. Consequence reported honestly: the node's identity
    // (NodeId) CHANGES - outbound links are re-created, but INBOUND references
    // from elsewhere in the project to the moved subtree are NOT rewritten.
    //
    // ONE NODE KIND IS REFUSED OUTRIGHT: a NetLogic. See IsNetLogicNode.
    private string MoveNodeInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string newParent = QueryParam(firstLine, "new_parent");
        string newName = QueryParam(firstLine, "new_name");
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(newParent))
            return ErrorJson("bad_query", "required: path, new_parent (+ optional new_name)");
        var steps = new StringBuilder();
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            // BEFORE ANY MUTATION - a NetLogic move is not a slow failure, it is
            // an instant lost session. See IsNetLogicNode / NetLogicMoveMessage.
            if (IsNetLogicNode(node))
                return ErrorJson("refused_netlogic_move", NetLogicMoveMessage(path));
            if (!(node is IUAObject) || node.NodeClass != NodeClass.Object)
                return ErrorJson("not_an_object",
                    path + " is " + node.NodeClass + " - move handles Object instances" +
                    " (for a variable, recreate it; for a type, it has no layout position)");
            var parentNode = ResolveNode(newParent);
            if (parentNode == null || !(parentNode is IUAObject || parentNode is IUAObjectType))
                return ErrorJson("node_not_found", "no object or type at new_parent: " + newParent);
            var nodePath = NodePathOf(node);
            var parentPath = NodePathOf(parentNode);
            if (!string.IsNullOrEmpty(nodePath) &&
                (parentPath == nodePath || parentPath.StartsWith(nodePath + "/")))
                return ErrorJson("move_into_self",
                    "new_parent " + newParent + " is inside the subtree being moved");
            var name = string.IsNullOrEmpty(newName) ? node.BrowseName : newName;
            var dup = DupNameGuard(parentNode, name, newParent);
            if (dup != null) return dup;
            var srcType = (node as UAObject)?.ObjectType;
            if (srcType == null)
                return ErrorJson("not_an_object", path + " has no resolvable ObjectType");

            var newNode = InformationModel.MakeObject(name, srcType.NodeId);
            parentNode.Add(newNode);
            steps.Append("\"create_copy\"");
            int copied = 0;
            var skipped = new StringBuilder();
            var fixups = new List<LinkFixup>();
            string cerr = CopySubtreeInto(node, newNode, ref copied, skipped, fixups, 0);
            if (cerr != null)
            {
                newNode.Delete();
                return ErrorJson("copy_failed", cerr + " - nothing was changed (partial copy removed)");
            }
            ApplyLinkFixups(node, newNode, fixups, skipped);
            steps.Append(",\"copy_subtree\"");

            int absOk = 0, relUnverified = 0;
            var broken = new StringBuilder();
            AuditLinks(newNode, ref absOk, ref relUnverified, broken);

            node.Delete();
            steps.Append(",\"delete_original\"");

            return "{\"ok\":true,\"from\":\"" + JsonEscape(path) +
                   "\",\"to\":\"" + JsonEscape(newParent + "/" + name) +
                   "\",\"copied_nodes\":" + copied +
                   ",\"skipped\":[" + skipped + "]" +
                   ",\"links_verified\":" + absOk +
                   ",\"relative_links_unverified\":" + relUnverified +
                   ",\"broken_links\":[" + broken + "]" +
                   ",\"steps\":[" + steps + "]" +
                   ",\"note\":\"the moved node has a NEW NodeId - inbound references" +
                   " from elsewhere to the old subtree are not rewritten\"" +
                   ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"steps\":[" + steps + "],\"error\":\"" +
                   JsonEscape(ExcMsg(ex)) +
                   "\",\"nudge\":\"move stopped mid-way - inspect " + JsonEscape(path) +
                   " and " + JsonEscape(newParent) + " with describe_node before retrying\"}";
        }
    }

    // FIELD FINDING 2026-09-04: moving a NetLogicObject CLOSED THE PROJECT in
    // Studio outright - the bridge went with it, every unsaved edit was lost, and
    // there was no exception, no error body, nothing in the log. move works by
    // RE-AUTHORING a copy and deleting the original (see the header above), and a
    // NetLogic node carries no proxy and no code-reference property to re-author
    // (that is exactly what CreateNetLogicInline documents), so the copy is a
    // half-formed logic node and the delete takes the real one with it.
    //
    // Identified two ways, because either one alone can miss: the generated CLR
    // proxy for a NetLogic node is FTOptix.NetLogic.NetLogicObject, and its type
    // chain reaches FTOptix.NetLogic.ObjectTypes.NetLogic - the very NodeId
    // CreateNetLogicInline makes these nodes from.
    private static bool IsNetLogicNode(IUANode node)
    {
        try
        {
            for (var t = node.GetType(); t != null; t = t.BaseType)
                if (t.Name == "NetLogicObject") return true;
            IUAObjectType ot = (node as UAObject)?.ObjectType;
            for (int i = 0; ot != null && i < 20; i++, ot = ot.SuperType)
                if (ot.NodeId.Equals(FTOptix.NetLogic.ObjectTypes.NetLogic)) return true;
        }
        catch { /* a node we cannot classify is not refused */ }
        return false;
    }

    // One message for both the refusal and the dry-run report, so validate_ops
    // and the write path can never disagree about why a move was refused.
    private static string NetLogicMoveMessage(string path)
    {
        return path + " is a NetLogic node - move is refused. Moving one closed the " +
               "project in Studio outright (2026-09-04: bridge gone, unsaved edits " +
               "lost, no exception raised). move re-authors a copy and deletes the " +
               "original, and a NetLogic node has neither a proxy nor a code " +
               "reference to re-author. Move it in Studio's Project view (drag the " +
               "node to its new parent), save, then re-arm the bridge.";
    }

    // A DynamicLink found during a subtree copy, applied AFTER the whole copy
    // completes: an intra-subtree link's target may be a sibling copied LATER,
    // and pointing the copy at the ORIGINAL node breaks the moment the
    // original is deleted (found designing move; latent in convert too).
    private class LinkFixup
    {
        public IUAVariable DstVar;
        public string Raw;
        public string SrcVarPath;
    }

    // Recursive re-author copy: for each Object child, a fresh MakeObject of
    // the SAME ObjectType added to dst (born-in-place); for each Variable
    // child, a same-name variable on dst with the raw UAValue copied (no
    // coercion - same datatype by construction). DynamicLinks are RECORDED
    // into `fixups` for ApplyLinkFixups, not applied inline. Returns null on
    // success, error string to abort. Unsupported constructs go into `skipped`
    // (json fragments), not half-copied.
    private string CopySubtreeInto(IUANode src, IUANode dst, ref int copied,
                                   StringBuilder skipped, List<LinkFixup> fixups, int depth)
    {
        if (depth > 12) return "subtree deeper than 12 levels at " + NodePathOf(src);
        foreach (var c in src.Children)
        {
            if (c is IUAVariable sv)
            {
                var cn = c.GetType().Name;
                if (cn == "DynamicLink") continue;   // handled via the owner var's fixup
                IUAVariable nv = null;
                if (dst is IUAObject dObj) nv = dObj.GetOrCreateVariable(c.BrowseName);
                else if (dst is IUAObjectType dTyp) nv = dTyp.GetOrCreateVariable(c.BrowseName);
                if (nv == null)
                {
                    var mv = InformationModel.MakeVariable(c.BrowseName, sv.DataType);
                    dst.Add(mv);
                    nv = mv;
                }
                try { if (sv.Value != null && !IsArrayVariable(sv)) nv.Value = sv.Value; }
                catch (Exception ex)
                {
                    if (skipped.Length > 0) skipped.Append(",");
                    skipped.Append("\"value of " + JsonEscape(c.BrowseName) + ": " + JsonEscape(ex.Message) + "\"");
                }
                foreach (var lc in sv.Children)
                {
                    if (lc.GetType().Name == "DynamicLink" && lc is IUAVariable lv && lv.Value != null)
                    {
                        var raw = lv.Value.Value as string;
                        if (!string.IsNullOrEmpty(raw))
                            fixups.Add(new LinkFixup { DstVar = nv, Raw = raw, SrcVarPath = NodePathOf(sv) });
                    }
                    else if (lc.GetType().Name != "DynamicLink")
                    {
                        if (skipped.Length > 0) skipped.Append(",");
                        skipped.Append("\"" + JsonEscape(sv.BrowseName + "/" + lc.BrowseName) +
                                       " (" + JsonEscape(lc.GetType().Name) + "): not copied (converter/attachment)\"");
                    }
                }
                copied++;
            }
            else if (c is IUAObject so)
            {
                var sot = (so as UAObject)?.ObjectType;
                if (sot == null)
                {
                    if (skipped.Length > 0) skipped.Append(",");
                    skipped.Append("\"" + JsonEscape(c.BrowseName) + ": no resolvable ObjectType\"");
                    continue;
                }
                var no = InformationModel.MakeObject(c.BrowseName, sot.NodeId);
                dst.Add(no);
                copied++;
                var r = CopySubtreeInto(c, no, ref copied, skipped, fixups, depth + 1);
                if (r != null) return r;
            }
            else
            {
                if (skipped.Length > 0) skipped.Append(",");
                skipped.Append("\"" + JsonEscape(c.BrowseName) + " (" + c.NodeClass + "): unsupported node class\"");
            }
        }
        return null;
    }

    // Apply recorded DynamicLinks after a completed subtree copy.
    // - RELATIVE ("../..") and brace-form ("{Alias}/...") raws are reproduced
    //   VERBATIM (SetDynamicLink(null) + raw write): the copy occupies the same
    //   relative position, so the same literal stays correct - this is exactly
    //   why Studio stores template links relative.
    // - ABSOLUTE ("/Objects/<proj>/...") raws are resolved: a target INSIDE the
    //   source subtree is REMAPPED to its counterpart in the destination (the
    //   original may be about to be deleted); an external target is linked
    //   directly. Unresolvable -> skipped, never guessed.
    private void ApplyLinkFixups(IUANode srcRoot, IUANode dstRoot,
                                 List<LinkFixup> fixups, StringBuilder skipped)
    {
        string srcRootPath = NodePathOf(srcRoot);
        string absPrefix = "/Objects/" + Project.Current.BrowseName + "/";
        foreach (var f in fixups)
        {
            try
            {
                if (!f.Raw.StartsWith(absPrefix))
                {
                    // Relative / brace / attribute form: verbatim literal.
                    f.DstVar.SetDynamicLink(null, DynamicLinkMode.Read);
                    var dlv = f.DstVar.Refs.GetVariable(FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                    if (dlv != null) dlv.Value = f.Raw;
                    else throw new Exception("link materialization returned null");
                    continue;
                }
                var rel = f.Raw.Substring(absPrefix.Length);
                IUAVariable target;
                if (!string.IsNullOrEmpty(srcRootPath) &&
                    (rel == srcRootPath || rel.StartsWith(srcRootPath + "/")))
                {
                    // Intra-subtree: remap to the copy's counterpart.
                    var inner = rel == srcRootPath ? "" : rel.Substring(srcRootPath.Length + 1);
                    IUANode t = dstRoot;
                    foreach (var seg in inner.Split('/'))
                    {
                        if (seg.Length == 0) continue;
                        t = t?.Get(seg);
                    }
                    target = t as IUAVariable;
                }
                else
                {
                    target = ResolveNode(rel) as IUAVariable;
                }
                if (target == null)
                    throw new Exception("target not re-resolvable: " + f.Raw);
                f.DstVar.SetDynamicLink(target);
            }
            catch (Exception ex)
            {
                if (skipped.Length > 0) skipped.Append(",");
                skipped.Append("\"link from " + JsonEscape(f.SrcVarPath) + ": " + JsonEscape(ex.Message) + "\"");
            }
        }
    }

    // Walk a subtree; verify project-absolute DynamicLinks still resolve, count
    // owner-relative ones (unverifiable without replicating Optix path semantics).
    private void AuditLinks(IUANode root, ref int absOk, ref int relUnverified, StringBuilder broken)
    {
        foreach (var c in root.Children)
        {
            if (c.GetType().Name == "DynamicLink" && c is IUAVariable lv && lv.Value != null)
            {
                var raw = lv.Value.Value as string;
                if (!string.IsNullOrEmpty(raw))
                {
                    var absPrefix = "/Objects/" + Project.Current.BrowseName + "/";
                    if (raw.StartsWith(absPrefix))
                    {
                        var rel = raw.Substring(absPrefix.Length);
                        var at = rel.IndexOf('@');
                        var target = ResolveNode(at >= 0 ? rel.Substring(0, at).TrimEnd('/') : rel);
                        if (target != null) absOk++;
                        else
                        {
                            if (broken.Length > 0) broken.Append(",");
                            broken.Append("\"" + JsonEscape(NodePathOf(c) + " -> " + rel) + "\"");
                        }
                    }
                    else if (raw.StartsWith(".")) relUnverified++;
                }
            }
            AuditLinks(c, ref absOk, ref relUnverified, broken);
        }
    }

    // Placeholder-collection routing: parents whose schema
    // declares a PlaceholderChildNodeCollection property (NavigationPanel.Panels,
    // DataGrid.Columns, ListView.TypeSelectors, Trend/XYChart.Pens, gauges'
    // WarningZones) need children placed INSIDE the named collection, the way
    // Studio's own drag-and-drop does - a flat Children.Add "succeeds" but the
    // emulator fails on it (the NavigationPanel.Panels incident). Reflection-
    // driven, no hardcoded type table: a property routes when its CLR type is
    // PlaceholderChildNodeCollection`1 AND its generic element type accepts the
    // widget being created. Read-only variant (PlaceholderReadOnlyChildNode-
    // Collection`1, e.g. Trend.TimeRanges) is runtime-managed: never a target.
    private const string PlaceholderCollPrefix = "PlaceholderChildNodeCollection";
    private const string PlaceholderRoCollPrefix = "PlaceholderReadOnlyChildNodeCollection";

    private static bool IsPlaceholderColl(Type pt)
    { return pt.Name.StartsWith(PlaceholderCollPrefix); }

    private static bool IsPlaceholderRoColl(Type pt)
    { return pt.Name.StartsWith(PlaceholderRoCollPrefix); }

    private static Type PlaceholderElementType(Type pt)
    {
        return pt.IsGenericType && pt.GetGenericArguments().Length == 1
            ? pt.GetGenericArguments()[0] : null;
    }

    // Collection properties on `parent` whose element type accepts `childClr`.
    // readOnly selects the runtime-managed variant (for the loud rejection).
    private static List<string> MatchingPlaceholderColls(
        IUANode parent, Type childClr, bool readOnly)
    {
        var hits = new List<string>();
        if (parent == null || childClr == null) return hits;
        foreach (var pi in parent.GetType().GetProperties(
                     BindingFlags.Public | BindingFlags.Instance))
        {
            bool ro = IsPlaceholderRoColl(pi.PropertyType);
            if (readOnly ? !ro : (ro || !IsPlaceholderColl(pi.PropertyType))) continue;
            var elem = PlaceholderElementType(pi.PropertyType);
            if (elem != null && elem.IsAssignableFrom(childClr)) hits.Add(pi.Name);
        }
        return hits;
    }

    // POST /bridge/ui/widget?name=X&screen=UI/MainWindow&type=Label
    // Creates a UI object of a builtin type and adds it to a screen, INLINE on
    // the HTTP thread. The more thread-sensitive write (presentation engine).
    private string WriteWidgetInline(string firstLine)
    {
        string name = QueryParam(firstLine, "name");
        string screen = QueryParam(firstLine, "screen") ?? "UI/MainWindow";
        string typeName = QueryParam(firstLine, "type") ?? "Label";
        if (string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "missing required query param: name");
        try
        {
            bool _viaBrowseName;
            var typeId = ResolveUiTypeId(typeName, out _viaBrowseName);
            if (typeId == null)
                return ErrorJson("type_not_found", "no builtin UI type: " + typeName);
            // The ObjectTypes catalog also carries the ABSTRACT layout bases in the
            // Item -> Container -> Panel chain. `Item`/`Container` are not concrete
            // renderable widgets: a bare instance is "not a UI object type" to the
            // WebPresentationEngine and CRASHES the render tree, killing every
            // sibling after it (found live 2026-07-25 -- an agent picked "Container"
            // as a layout widget and lost the screen). Refuse loud and redirect.
            // (A full reflect-the-runtime-proxy renderability filter is a follow-up;
            // these two are the only bases an author realistically mistakes for a
            // widget.)
            if (typeName == "Item" || typeName == "Container")
                return ErrorJson("not_renderable",
                    "'" + typeName + "' is an abstract layout base, not a renderable "
                    + "widget -- a bare instance crashes the render tree. Use 'Panel' "
                    + "(invisible layout container; add a Rectangle child for a "
                    + "background) or 'Rectangle' (a filled/bordered box) instead.");
            var screenNode = ResolveNode(screen);
            if (screenNode == null)
                return ErrorJson("node_not_found", "no screen at: " + screen);
            // Screens must be ObjectTypes (a SUBTYPE of Screen) to load at runtime.
            // Studio's right-click "Add Screen" makes a TYPE; a MakeObject makes an
            // INSTANCE that previews in the designer but the runtime can't instantiate
            // as a loadable panel. typeinfo-confirmed: right-click Screen1 =
            // ObjectType/ScreenType inheriting Screen->Panel->Container->Item; a bridge
            // MakeObject was Object/Screen (a bare instance). MakeObjectType<Screen>
            // reproduces the right-click structure. Child widgets then add to the TYPE.
            if (typeName == "Screen")
            {
                var st = InformationModel.MakeObjectType<FTOptix.UI.ScreenType>(name);
                screenNode.Children.Add(st);
                return "{\"ok\":true,\"created_path\":\"" + JsonEscape(screen + "/" + name) +
                       "\",\"type\":\"Screen\",\"kind\":\"objecttype\",\"node_class\":\"" +
                       st.NodeClass + "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
            }
            // Duplicate-name guard: Optix happily creates same-name siblings,
            // which are then impossible to address (or delete) unambiguously
            // by path. Studio's own UI auto-suffixes; the bridge refuses loud.
            foreach (var existing in screenNode.Children)
            {
                if (existing.BrowseName == name)
                    return ErrorJson("name_exists",
                        "a node named '" + name + "' already exists under '" +
                        screen + "' - pick a different name or delete it first");
            }
            // Explicit collection-path targeting stays authoritative - but a
            // READ-ONLY collection is rejected loud, never silently accepted.
            // The resolved NODE's proxy type is not the collection type (that's
            // the parent's declared PROPERTY type),
            // so the check goes through the owner's property of the same name.
            var ownerNode = screenNode.Owner;
            if (ownerNode != null)
            {
                var ownerProp = ownerNode.GetType().GetProperty(
                    screenNode.BrowseName, BindingFlags.Public | BindingFlags.Instance);
                if (ownerProp != null && IsPlaceholderRoColl(ownerProp.PropertyType))
                    return ErrorJson("read_only_collection",
                        "collection at '" + screen + "' is runtime-managed (read-only) - " +
                        "children cannot be authored into it");
            }
            // Placement decision BEFORE creating the widget (no orphan node on
            // a refusal). 0 matches -> today's flat add; 1 -> auto-route into
            // the collection; >1 -> refuse, caller must pass the explicit path.
            var childClr = ResolveWidgetClrType(typeName);
            var routes = MatchingPlaceholderColls(screenNode, childClr, readOnly: false);
            if (routes.Count > 1)
                return ErrorJson("ambiguous_container",
                    "type '" + typeName + "' fits multiple collections on '" + screen +
                    "': " + string.Join(", ", routes) +
                    " - pass the collection sub-path explicitly (e.g. " +
                    screen + "/" + routes[0] + ")");
            if (routes.Count == 0)
            {
                var roHits = MatchingPlaceholderColls(screenNode, childClr, readOnly: true);
                if (roHits.Count > 0)
                    return ErrorJson("read_only_collection",
                        "type '" + typeName + "' only fits runtime-managed (read-only) " +
                        "collection(s) on '" + screen + "': " + string.Join(", ", roHits) +
                        " - these cannot be authored into");
            }
            // GridLayoutProperties must land in the FTOptix.UI namespace; a generic
            // MakeObject would qualify the browse name with the project namespace and
            // the engine silently ignores RowStart/ColumnStart on it (bridge >= 1.0.8).
            if (name == "GridLayoutProperties")
                return TryCreateUiNamespaceChild(screenNode, name, screen);
            var widget = InformationModel.MakeObject(name, typeId);
            if (routes.Count == 1)
            {
                var collNode = ResolveNode(screen + "/" + routes[0]);
                if (collNode == null)
                    return ErrorJson("node_not_found",
                        "collection '" + routes[0] + "' declared by the type but not " +
                        "resolvable at: " + screen + "/" + routes[0]);
                collNode.Children.Add(widget);
                return "{\"ok\":true,\"created_path\":\"" +
                       JsonEscape(screen + "/" + routes[0] + "/" + name) +
                       "\",\"type\":\"" + JsonEscape(typeName) +
                       "\",\"routed_into\":\"" + JsonEscape(routes[0]) +
                       "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
            }
            screenNode.Children.Add(widget);
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(screen + "/" + name) +
                   "\",\"type\":\"" + JsonEscape(typeName) +
                   "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"mode\":\"inline\",\"error\":\"" +
                   JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // GENERIC property set: POST /bridge/node/property?path=<node>&name=<prop>&value=<v>
    // Resolves the property variable and assigns .Value coerced by its DataType.
    // One endpoint for ALL properties (Text/Color/Width/Model-var/...), no
    // per-component code. Coercion mirrors the cheatsheet's uniform .Value model.
    // POST /bridge/node/displayname?path=X&value=Y[&locale=en-US]
    // Set a node's DisplayName ATTRIBUTE (LocalizedText). This is a direct
    // node-attribute assignment - NOT the variable-materialization path that
    // crashed Studio (that fabricated an orphan UA variable named "DisplayName";
    // this writes the real attribute the proxy setter fronts). BrowseName stays
    // rename-only: it is the node's IDENTITY (paths, links, bindings key on it),
    // so it goes through move/new_name re-authoring, never in-place mutation.
    private string SetDisplayNameInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string raw = QueryParam(firstLine, "value");
        string locale = QueryParam(firstLine, "locale") ?? "en-US";
        if (string.IsNullOrEmpty(path) || raw == null)
            return ErrorJson("bad_query", "required query params: path, value");
        try
        {
            var node = ResolveNode(path);
            if (node == null)
                return ErrorJson("node_not_found", "no node at: " + path);
            string before = null;
            try { var d = node.DisplayName; if (d != null) before = d.Text; } catch { }
            node.DisplayName = new LocalizedText(raw, locale);
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path) +
                   "\",\"attribute\":\"DisplayName\"" +
                   ",\"locale\":\"" + JsonEscape(locale) + "\"" +
                   (before != null ? ",\"was\":\"" + JsonEscape(before) + "\"" : "") +
                   ",\"value\":\"" + JsonEscape(raw) +
                   "\",\"note\":\"browse_name (paths/links) is unchanged - Studio's " +
                   "tree shows 'BrowseName (DisplayName)' when they differ\"" +
                   ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    private string SetPropertyInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string name = QueryParam(firstLine, "name");
        string raw = QueryParam(firstLine, "value") ?? "";
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "required query params: path, name");
        // DisplayName rides the set_property surface for caller convenience but
        // is handled by the attribute route - it must never reach the
        // variable-materialization path below.
        if (name == "DisplayName")
            return SetDisplayNameInline(firstLine);
        var fmtErr = FormatSpecifierError(name, raw);
        if (fmtErr != null) return ErrorJson("bad_value", fmtErr);
        try
        {
            var node = ResolveNode(path);
            if (node == null)
                return ErrorJson("node_not_found", "no node at: " + path);

            // Set a VARIABLE's OWN value: `set Model/MyVar Value=...`. A model variable's
            // value is the node itself, not a child "Value" property, so GetVariable(name)
            // below would miss it. (Only fires when the resolved NODE is a variable - a
            // widget with a child "Value" prop like SpinBox is an IUAObject, unaffected.)
            if (name == "Value" && node is IUAVariable selfVar)
            {
                if (IsArrayVariable(selfVar))
                    return ArrayWriteError(name, "variable " + path, DataTypeName(selfVar));
                var err = CoerceAssign(selfVar, raw, firstLine);
                if (err != null) return ErrorJson("bad_value", err);
                return PropOkJson(path, name, DataTypeName(selfVar), "self", selfVar);
            }

            var prop = node.GetVariable(name);
            if (prop != null)
            {
                // Already-materialized variable (model vars, or UI props set before).
                if (IsArrayVariable(prop))
                    return ArrayWriteError(name, node.GetType().Name, DataTypeName(prop));
                var err = CoerceAssign(prop, raw, firstLine);
                if (err != null) return ErrorJson("bad_value", err);
                return PropOkJson(path, name, DataTypeName(prop), "variable", prop);
            }

            // VALIDITY GATE (crash-safety). GetOrCreateVariable below happily
            // FABRICATES an orphan variable for a property the TYPE does not declare
            // (e.g. Panel.BorderThickness - Panels have no border). The renderer then
            // dereferences the orphan off-thread and access-violates, killing Studio
            // outright (0xC0000005 in coreclr, confirmed live). node.GetType()
            // is the generated Optix proxy and GetProperty is inheritance-aware, so a
            // Rectangle exposes BorderColor but a Panel does not. Reject an undeclared
            // property BEFORE materializing it, and hand back the valid set so the
            // caller (or describe_*) can self-correct instead of crashing.
            var gateErr = DeclaredPropertyGuard(node, name);
            if (gateErr != null) return gateErr;

            // ARRAY GATE (crash-safety). Must fire BEFORE GetOrCreateVariable: the
            // NodeId[] AliasNodeArray crash (confirmed live 2026-07-16 - Studio
            // process terminated, connection reset mid-request, no managed
            // exception) fired at/after this point, so array-ness has to come from
            // the TYPE DECLARATION, not the never-yet-materialized variable.
            var arrErr = DeclaredArrayGuard(node, name);
            if (arrErr != null) return arrErr;

            // FRESH-INSTANCE MATERIALIZATION (node-model, off-thread-safe).
            // GetVariable was null - a fresh MakeObject'd instance doesn't materialize
            // its inherited props. node.GetOrCreateVariable(name) creates the variable
            // FROM THE TYPE DECLARATION (built-in; ref RPC-Template/ProjectOptimizer.cs)
            // so it renders, using only node-model ops which ARE off-thread-safe. We do
            // NOT use the typed CLR setter (hard-crashes off-thread) nor DelayedTask
            // marshaling (HANGS at design time - Studio has no task pump; both confirmed
            // live). Safe here ONLY because the gate above proved the
            // property is type-declared.
            // ObjectTypes materialize too (same branch as attach-conditional and
            // bind-property). Without it, a static on a fresh ObjectType member fell
            // through to property_not_found and the only route was the bind_property
            // (junk raw link) -> delete DynamicLink -> set_property dance
            // (confirmed live 2026-08-31, ToggleSwitch authoring).
            IUAVariable mvar = null;
            if (node is IUAObject asObject)
                mvar = asObject.GetOrCreateVariable(name);
            else if (node is IUAObjectType asType)
                mvar = asType.GetOrCreateVariable(name);
            if (mvar == null)
                return ErrorJson("property_not_found",
                    "node " + path + " has no variable or materializable property " + name);
            if (IsArrayVariable(mvar))   // backstop: declared-array gate above should have caught it
                return ArrayWriteError(name, node.GetType().Name, DataTypeName(mvar));
            var merr = CoerceAssign(mvar, raw, firstLine);
            if (merr != null) return ErrorJson("bad_value", merr);
            return PropOkJson(path, name, DataTypeName(mvar), "materialized", mvar);
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" +
                   JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // ARRAY-WRITE GUARD (crash-safety). Array-typed UA variables (String[] like
    // GridLayout.Columns/Rows, NodeId[] like NavigationPanelItem.AliasNodeArray)
    // must never reach the scalar coercion/assign path: a scalar .Value assign
    // raises a catchable CoreException at best (String[], "array dimensions
    // mismatch") and has TERMINATED the Studio process outright at worst
    // (NodeId[] AliasNodeArray, confirmed live 2026-07-16 - connection reset
    // mid-request, no managed exception, all unsaved edits lost). Array-ness
    // lives in ArrayDimensions / the CLR proxy property type, NOT the DataType
    // name - a NodeId[] variable reports DataTypeName "NodeId" - so the
    // CoerceAssign datatype switch cannot see it and must be gated up front.
    private static bool IsArrayVariable(IUAVariable v)
    {
        try { var d = v.ArrayDimensions; return d != null && d.Length > 0; }
        catch { return false; }
    }

    // null unless `name` is a declared CLR-array property on the node's generated
    // proxy type (the proxy exposes array UA props as CLR arrays, e.g. NodeId[]
    // AliasNodeArray), else the unsupported_array_write error. Call BEFORE
    // GetOrCreateVariable - array-ness must be established from the declaration,
    // without materializing (and without writing) anything on the live model.
    private string DeclaredArrayGuard(IUANode node, string name)
    {
        var pi = node.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
                     .FirstOrDefault(p => p.Name == name);
        if (pi == null || !pi.PropertyType.IsArray) return null;
        return ArrayWriteError(name, node.GetType().Name, pi.PropertyType.GetElementType().Name);
    }

    // The .NET ENUM type a property really has, read off its owner's generated
    // CLR proxy rather than off the UA datatype's browse NAME. Null when the
    // owner declares no such property, or declares it non-enum. Companion to
    // DeclaredArrayGuard (same reflection, same reason: the declaration knows
    // things the live variable does not), and the I31 backstop for
    // ResolveEnumType, which can only match a datatype whose browse name IS a
    // loaded enum type name.
    private static Type DeclaredEnumType(IUAVariable v)
    {
        try
        {
            var owner = v.Owner;
            if (owner == null) return null;
            var pi = owner.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
                          .FirstOrDefault(p => p.Name == v.BrowseName);
            if (pi == null) return null;
            return pi.PropertyType.IsEnum ? pi.PropertyType : null;
        }
        catch { return null; }
    }

    private static string ArrayWriteError(string name, string owner, string elemType)
    {
        return "{\"error\":{\"code\":\"unsupported_array_write\",\"message\":\"" +
               JsonEscape("property '" + name + "' on " + owner + " is array-typed (" +
                          elemType + "[]). Array writes aren't supported via set_property - " +
                          "a scalar write to an array UA variable can crash Studio. " +
                          "Author array-valued properties in Studio directly.") + "\"}}";
    }

    // Coerce a raw string into a variable's value by the variable's own DataType, and
    // assign it. Returns null on success, or an error message. Single source for the
    // property-set coercion (was duplicated across two switch blocks).
    private string CoerceAssign(IUAVariable v, string raw, string firstLine)
    {
        string dt = DataTypeName(v);
        // Backstop for callers other than SetPropertyInline (which gates arrays
        // with the typed unsupported_array_write error before reaching here).
        if (IsArrayVariable(v))
            return "unsupported_array_write: property is array-typed (" + dt +
                   "[]); scalar writes to array UA variables are not supported" +
                   " (they can crash Studio)";
        switch (dt)
        {
            case "Boolean":
                v.Value = (raw == "true" || raw == "1" || raw == "True"); break;
            case "Int16":
            case "Int32":
            case "Int64":
            case "UInt16":
            case "UInt32":
            case "UInt64":
            case "Byte":
            case "SByte":
                {
                    // Parse to the declared width. A blanket (int) cast silently wrapped
                    // Int64/UInt64 mod 2^32 (123456789012345 stored as -2045911175).
                    UAValue iv;
                    string err = ParseInteger(dt, raw, out iv);
                    if (err != null) return err;
                    v.Value = iv;
                }
                break;
            case "Float":
            case "Double":
            case "Size":
                {
                    double dv;
                    if (!double.TryParse(raw, System.Globalization.NumberStyles.Float,
                                         System.Globalization.CultureInfo.InvariantCulture, out dv))
                        return "value must be a number for " + dt + ": " + raw;
                    v.Value = dv;
                }
                break;
            case "LocalizedText":
                // LITERAL/ad-hoc text: new LocalizedText(text, locale) renders directly;
                // the (nsIndex, textId) form is a translation KEY (blank without a table
                // entry) - use /bridge/i18n/translation for keyed.
                v.Value = new LocalizedText(raw, QueryParam(firstLine, "locale") ?? "en-US"); break;
            case "String":
                v.Value = raw; break;
            case "NodeId":
                // A NodePointer / NodeId property (PanelLoader.Panel, StartWindow, ...).
                // The generic coercion can't string->NodeId ("Conversion to NodeId not
                // supported"), so resolve the value as a node PATH -> NodeId, mirroring
                // ensure_web_engine's StartWindow set. This is what lets a content loader
                // point at a screen (the nav "empty content" root cause).
                {
                    // "" / "null" CLEARS the pointer. A NodePointer driven by
                    // a dynamic link keeps its old static Value forever
                    // otherwise - invisible on screen (the link wins) but
                    // misleading in the property grid, and the fallback if
                    // the link ever resolves to nothing.
                    if (IsNodeIdClearToken(raw)) { v.Value = NodeId.Empty; break; }
                    var target = ResolveNode(raw);
                    if (target == null)
                        return "NodeId value must be a resolvable node path: " + raw;
                    v.Value = target.NodeId;
                }
                break;
            case "Color":
                // Color is UInt32 ARGB. Accept "#RRGGBB" / "#AARRGGBB" hex or a decimal.
                // Without this it hit the enum-default path, where Convert.ToInt32
                // OVERFLOWS on any color with alpha (0xFF...... > Int32.Max) and falls to
                // the string->LocalizedText assertion ("!localeId.empty()") - so every
                // opaque color silently failed (found via live probing).
                {
                    string s = raw.Trim();
                    uint argb;
                    if (s.StartsWith("#"))
                    {
                        string hex = s.Substring(1);
                        if (hex.Length == 6) hex = "FF" + hex;   // add opaque alpha
                        if (!uint.TryParse(hex, System.Globalization.NumberStyles.HexNumber,
                                           System.Globalization.CultureInfo.InvariantCulture, out argb))
                            return "Color must be #RRGGBB / #AARRGGBB hex or a UInt32 decimal: " + raw;
                    }
                    else if (!uint.TryParse(s, out argb))
                    {
                        return "Color must be #RRGGBB / #AARRGGBB hex or a UInt32 decimal: " + raw;
                    }
                    v.Value = argb;
                }
                break;
            default:
                // Enum datatypes (HorizontalAlignment, ...) are Int32-backed; a bare-string
                // assign hits a LocalizedText coercion that asserts on an empty localeId
                // Coerce to the enum ordinal (int or friendly name),
                // else return a clean error (an INVALID enum value must NOT fall through to
                // the asserting string assign - confirmed live).
                {
                    var e = SetEnumOrRaw(v, dt, raw);
                    if (e != null) return e;
                }
                break;
        }
        return null;
    }

    // ---- U16: POST /bridge/validate_ops -------------------------------------
    //
    // Dry-run an op BATCH and report what would fail, without touching the model.
    // Every check here reuses a guard the real write path already runs, so a
    // clean report means the same guards will pass on apply - the point is to
    // fail the batch BEFORE it half-applies, not to re-implement the rules.
    //
    // Three tiers:
    //   1 per-op validity  - node resolves, property is declared, value coerces
    //   2 batch coherence  - ops are checked against a HYPOTHETICAL model that
    //                        accumulates this batch's creates/deletes/moves, so
    //                        "create X then set X.Prop" validates clean and the
    //                        reverse order does not
    //   3 lint             - warnings only; `strict` promotes them to errors
    //
    // Body: {"ops":[{"op":"...", ...}], "strict":false}
    // Reply: {"ok":bool, "op_count":N, "errors":[{op_index,code,message,...}],
    //         "warnings":[{op_index,code,message}]}
    //
    // Hypothetical tree state (Tier 2):
    //
    //   BEFORE (U16 original):
    //     created : Dictionary<string, string>   path -> declared-type-name
    //     (no move tracking)
    //
    //   AFTER (this revision):
    //     created  : Dictionary<string, HypoNode>  path -> {Verb, Type}
    //                records the op verb that created the node (create_widget,
    //                create_node, ...) in addition to its declared type, so
    //                downstream logic can distinguish widget-created nodes.
    //     relocated: Dictionary<string, string>   oldPath -> newPath
    //                set by every successful move op; used to rewrite prefix
    //                lookups for subsequent ops so "move X then set X.Prop"
    //                is treated as "set newX.Prop" rather than unresolved.
    //
    //   Resolution order (HypoResolve):
    //     1. Apply any relocated-prefix rewrite (longest-prefix match).
    //     2. Check the created dict under the (possibly rewritten) path.
    //     3. Try ResolveNode (the live model).
    //     4. Return null -> unresolved.
    //
    //   Why every op case calls HypoResolve instead of raw ResolveNode:
    //     A move op earlier in the batch changes the effective address of the
    //     moved node AND every descendant.  Raw ResolveNode still finds the
    //     pre-move address (the live model is not mutated during validation),
    //     so any subsequent op on the moved path would falsely resolve --
    //     passing validation but failing on apply.  HypoResolve applies the
    //     relocated rewrite first, so the post-move address is used.

    // Hypothetical node entry: tracks both the op verb that created the node
    // and its declared type name.  The verb distinguishes create_widget nodes
    // (which may have been routed into a child collection) from other creates.
    private class HypoNode
    {
        public string Verb;
        public string Type;
    }

    // Resolve a path against the hypothetical tree: apply any relocated-prefix
    // rewrite, then check the created set, then fall back to the live model.
    // Returns the resolved (post-rewrite) path when the node exists in any of
    // those layers, or null when nothing resolves it.
    private string HypoResolve(
        string path,
        Dictionary<string, HypoNode> created,
        Dictionary<string, string> relocated)
    {
        if (string.IsNullOrEmpty(path)) return null;

        // 1. Apply the longest matching relocated-prefix rewrite.
        string rewritten = path;
        int bestLen = -1;
        foreach (var kv in relocated)
        {
            string old = kv.Key;
            if (string.Equals(path, old, StringComparison.OrdinalIgnoreCase))
            {
                // Exact match -- the path itself was moved; further ops on it
                // must use the NEW address.  Return the new path (non-null)
                // to confirm the node still exists; callers that want to flag
                // "moved away" inspect relocated directly.
                if (old.Length > bestLen) { bestLen = old.Length; rewritten = kv.Value; }
            }
            else if (path.StartsWith(old + "/", StringComparison.OrdinalIgnoreCase) &&
                     old.Length > bestLen)
            {
                bestLen = old.Length;
                rewritten = kv.Value + path.Substring(old.Length);
            }
        }

        // 2. Check the hypothetical created set.
        if (created.ContainsKey(rewritten)) return rewritten;

        // 3. Fall back to the live model.
        if (ResolveNode(rewritten) != null) return rewritten;

        return null;
    }

    private string ValidateOpsJson(string body)
    {
        var errors = new StringBuilder();
        var warnings = new StringBuilder();
        int errCount = 0, warnCount = 0, opCount = 0;

        Action<int, string, string, string> addErr = (idx, code, msg, extra) =>
        {
            if (errCount++ > 0) errors.Append(",");
            errors.Append("{\"op_index\":" + idx + ",\"code\":\"" + JsonEscape(code) +
                          "\",\"message\":\"" + JsonEscape(msg) + "\"" +
                          (string.IsNullOrEmpty(extra) ? "" : "," + extra) + "}");
        };
        Action<int, string, string> addWarn = (idx, code, msg) =>
        {
            if (warnCount++ > 0) warnings.Append(",");
            warnings.Append("{\"op_index\":" + idx + ",\"code\":\"" + JsonEscape(code) +
                            "\",\"message\":\"" + JsonEscape(msg) + "\"}");
        };

        try
        {
            if (string.IsNullOrWhiteSpace(body))
                return ErrorJson("bad_body", "validate_ops requires a JSON body: {\"ops\":[...]}");

            using (var doc = System.Text.Json.JsonDocument.Parse(body))
            {
                var root = doc.RootElement;
                System.Text.Json.JsonElement opsEl;
                if (!root.TryGetProperty("ops", out opsEl) ||
                    opsEl.ValueKind != System.Text.Json.JsonValueKind.Array)
                    return ErrorJson("bad_body", "body must carry an \"ops\" array");

                bool strict = false;
                System.Text.Json.JsonElement strictEl;
                if (root.TryGetProperty("strict", out strictEl) &&
                    strictEl.ValueKind == System.Text.Json.JsonValueKind.True) strict = true;

                // Tier 2 state: the hypothetical model this batch would build.
                var created  = new Dictionary<string, HypoNode>(StringComparer.OrdinalIgnoreCase);
                var deleted  = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                // relocated: old absolute path -> new absolute path, filled by move ops.
                var relocated = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

                // PRE-PASS: every path this batch creates, regardless of position.
                // The sequential `created` set above is what decides validity (a
                // forward reference IS an error). This set exists only to make the
                // error ACTIONABLE: the reversed-order mistake is by definition a
                // forward reference, so at the point it is caught the sequential
                // set is still empty and could not name the culprit.
                // For create_widget we compute the routed path the same way the
                // sequential pass does (WillCreatePath helper) so NearestHint
                // names the actual path the node will land at.
                var willCreate = new List<string>();
                foreach (var pre in opsEl.EnumerateArray())
                {
                    string pv = JsonStr(pre, "op");
                    if (pv == null || !pv.StartsWith("create", StringComparison.Ordinal)) continue;
                    string pn = JsonStr(pre, "name");
                    if (string.IsNullOrEmpty(pn)) continue;
                    string pp = ParentKey(pre);
                    string basePath = string.IsNullOrEmpty(pp) ? pn : pp.TrimEnd('/') + "/" + pn;
                    if (pv == "create_widget" && !string.IsNullOrEmpty(pp))
                    {
                        // Best-effort: resolve the screen node and check routing.
                        // If it resolves we can name the actual collection path;
                        // if not (forward-ref screen) we fall back to the flat path.
                        var screenNode = ResolveNode(pp);
                        string tn = TypeKey(pre);
                        if (screenNode != null && !string.IsNullOrEmpty(tn))
                        {
                            var clr = ResolveWidgetClrType(tn);
                            if (clr != null)
                            {
                                var routes = MatchingPlaceholderColls(screenNode, clr, readOnly: false);
                                if (routes.Count == 1)
                                    basePath = pp.TrimEnd('/') + "/" + routes[0] + "/" + pn;
                            }
                        }
                    }
                    willCreate.Add(basePath);
                }

                int idx = -1;
                foreach (var op in opsEl.EnumerateArray())
                {
                    idx++; opCount++;
                    string verb = JsonStr(op, "op");
                    if (string.IsNullOrEmpty(verb))
                    {
                        addErr(idx, "missing_op", "op object has no \"op\" field", null);
                        continue;
                    }
                    string path = JsonStr(op, "path");
                    string name = JsonStr(op, "name");
                    string typeName = TypeKey(op);
                    string value = JsonStr(op, "value");

                    switch (verb)
                    {
                        case "set_property":
                        case "bind":
                        case "attach_expression":
                        case "attach_formatter":
                        case "attach_string_formatter":
                            // moved_earlier_in_batch: a path that was moved away in
                            // this batch is no longer valid at the old address.
                            if (!string.IsNullOrEmpty(path) && relocated.ContainsKey(path))
                            {
                                addErr(idx, "moved_earlier_in_batch",
                                       verb + " targets '" + path + "', which was moved earlier in this batch (now at '" + relocated[path] + "')",
                                       null);
                                break;
                            }
                            ValidateOnNode(idx, verb, path, name, value, created, deleted,
                                           relocated, willCreate, addErr, addWarn);
                            break;

                        case "delete":
                            {
                                if (string.IsNullOrEmpty(path))
                                { addErr(idx, "bad_op", verb + " requires \"path\"", null); break; }
                                if (deleted.Contains(path))
                                { addErr(idx, "already_deleted", "op deletes '" + path + "' twice in this batch", null); break; }
                                if (relocated.ContainsKey(path))
                                { addErr(idx, "moved_earlier_in_batch",
                                         "delete targets '" + path + "', which was moved earlier in this batch (now at '" + relocated[path] + "')", null); break; }
                                if (HypoResolve(path, created, relocated) == null)
                                    addErr(idx, "unresolved_reference",
                                           "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                                deleted.Add(path);
                                created.Remove(path);
                            }
                            break;

                        case "retype":
                            {
                                if (string.IsNullOrEmpty(path))
                                { addErr(idx, "bad_op", "retype requires \"path\"", null); break; }
                                if (deleted.Contains(path))
                                { addErr(idx, "modifies_deleted_node", "retype targets '" + path + "', deleted earlier in this batch", null); break; }
                                if (relocated.ContainsKey(path))
                                { addErr(idx, "moved_earlier_in_batch",
                                         "retype targets '" + path + "', which was moved earlier in this batch (now at '" + relocated[path] + "')", null); break; }
                                // retype allows loose resolution (slash-in-BrowseName)
                                if (ResolveNodeLoose(path) == null && HypoResolve(path, created, relocated) == null)
                                    addErr(idx, "unresolved_reference",
                                           "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                            }
                            break;

                        case "reorder":
                        case "wire_event":
                            {
                                if (string.IsNullOrEmpty(path))
                                { addErr(idx, "bad_op", verb + " requires \"path\"", null); break; }
                                if (deleted.Contains(path))
                                { addErr(idx, "modifies_deleted_node", verb + " targets '" + path + "', deleted earlier in this batch", null); break; }
                                if (relocated.ContainsKey(path))
                                { addErr(idx, "moved_earlier_in_batch",
                                         verb + " targets '" + path + "', which was moved earlier in this batch (now at '" + relocated[path] + "')", null); break; }
                                if (HypoResolve(path, created, relocated) == null)
                                    addErr(idx, "unresolved_reference",
                                           "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                            }
                            break;

                        case "move":
                            {
                                if (string.IsNullOrEmpty(path))
                                { addErr(idx, "bad_op", "move requires \"path\"", null); break; }
                                if (deleted.Contains(path))
                                { addErr(idx, "modifies_deleted_node", "move targets '" + path + "', deleted earlier in this batch", null); break; }
                                if (relocated.ContainsKey(path))
                                { addErr(idx, "moved_earlier_in_batch",
                                         "move targets '" + path + "', which was moved earlier in this batch (now at '" + relocated[path] + "')", null); break; }

                                // Resolve the source node (hypothetical or live).
                                var live = ResolveNode(path);
                                if (HypoResolve(path, created, relocated) == null)
                                {
                                    addErr(idx, "unresolved_reference",
                                           "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                                    break;
                                }
                                // Reuse the write path's own refusal (see
                                // MoveNodeInline): a batch must fail the NetLogic
                                // move in the DRY RUN, not discover it by losing
                                // the Studio session mid-apply.
                                if (live != null && IsNetLogicNode(live))
                                {
                                    addErr(idx, "refused_netlogic_move", NetLogicMoveMessage(path), null);
                                    break;
                                }

                                // Determine the destination.
                                string newParent = JsonStr(op, "new_parent");
                                string newNameField = JsonStr(op, "new_name");
                                string effectiveName = string.IsNullOrEmpty(newNameField)
                                    ? (path.LastIndexOf('/') >= 0
                                        ? path.Substring(path.LastIndexOf('/') + 1)
                                        : path)
                                    : newNameField;

                                if (string.IsNullOrEmpty(newParent))
                                {
                                    // new_parent is required for the hypothetical state
                                    // to track the destination; without it we cannot
                                    // record relocated and the moved-earlier guard would
                                    // miss subsequent ops on the old path.
                                    addErr(idx, "bad_op",
                                           "move requires \"new_parent\" so the batch validator " +
                                           "can track the node's new address", null);
                                    break;
                                }

                                // Validate that the destination parent exists.
                                if (HypoResolve(newParent, created, relocated) == null)
                                {
                                    addErr(idx, "unresolved_parent",
                                           "no parent node at '" + newParent + "'" +
                                           NearestHint(newParent, willCreate), null);
                                    break;
                                }

                                string newPath = newParent.TrimEnd('/') + "/" + effectiveName;

                                // Warn if the destination path is already occupied and
                                // the batch has not deleted it (delete-then-move credit).
                                if (HypoResolve(newPath, created, relocated) != null &&
                                    !deleted.Contains(newPath))
                                    addWarn(idx, "already_exists",
                                            "'" + newPath + "' already exists; the move may collide");

                                // Record the move so subsequent ops that reference the
                                // old path get moved_earlier_in_batch and ops that use
                                // the new path resolve correctly.
                                relocated[path] = newPath;

                                // Re-key any hypothetical nodes that were created under
                                // the old prefix so their new addresses are reachable.
                                var toRekey = new List<KeyValuePair<string, HypoNode>>();
                                foreach (var kv in created)
                                {
                                    if (kv.Key.StartsWith(path + "/",
                                            StringComparison.OrdinalIgnoreCase) ||
                                        string.Equals(kv.Key, path,
                                            StringComparison.OrdinalIgnoreCase))
                                        toRekey.Add(kv);
                                }
                                foreach (var kv in toRekey)
                                {
                                    created.Remove(kv.Key);
                                    string suffix = kv.Key.Length > path.Length
                                        ? kv.Key.Substring(path.Length) : "";
                                    created[newPath + suffix] = kv.Value;
                                }
                            }
                            break;

                        case "create_node":
                        case "create_variable":
                        case "create_folder":
                        case "create_object":
                        case "create_type":
                        case "create_alias":
                            {
                                if (string.IsNullOrEmpty(name))
                                { addErr(idx, "bad_op", verb + " requires \"name\"", null); break; }
                                string parentPath = ParentKey(op);
                                string newPath = string.IsNullOrEmpty(parentPath)
                                    ? name : parentPath.TrimEnd('/') + "/" + name;

                                if (created.ContainsKey(newPath))
                                { addErr(idx, "duplicate_create", "'" + newPath + "' is created twice in this batch", null); break; }
                                if (!string.IsNullOrEmpty(parentPath))
                                {
                                    if (deleted.Contains(parentPath))
                                        addErr(idx, "modifies_deleted_node",
                                               "parent '" + parentPath + "' is deleted earlier in this batch", null);
                                    else if (HypoResolve(parentPath, created, relocated) == null)
                                        addErr(idx, "unresolved_parent",
                                               "no parent node at '" + parentPath + "'" + NearestHint(parentPath, willCreate), null);
                                }
                                // Credit an earlier delete in THIS batch: a
                                // delete-then-recreate of the same path is the
                                // documented "hypothetical model accumulates
                                // creates AND deletes" contract, so it must not
                                // warn already_exists (which becomes an ERROR
                                // under strict). Only a create over a node the
                                // batch has NOT deleted may collide.
                                if (ResolveNode(newPath) != null && !deleted.Contains(newPath))
                                    addWarn(idx, "already_exists",
                                            "'" + newPath + "' already exists in the live model; the create may collide");
                                created[newPath] = new HypoNode { Verb = verb, Type = typeName ?? "" };
                                deleted.Remove(newPath);
                            }
                            break;

                        case "create_widget":
                            {
                                if (string.IsNullOrEmpty(name))
                                { addErr(idx, "bad_op", "create_widget requires \"name\"", null); break; }
                                string parentPath = ParentKey(op);
                                string basePath = string.IsNullOrEmpty(parentPath)
                                    ? name : parentPath.TrimEnd('/') + "/" + name;

                                if (created.ContainsKey(basePath))
                                { addErr(idx, "duplicate_create", "'" + basePath + "' is created twice in this batch", null); break; }
                                if (!string.IsNullOrEmpty(parentPath))
                                {
                                    if (deleted.Contains(parentPath))
                                    { addErr(idx, "modifies_deleted_node",
                                             "parent '" + parentPath + "' is deleted earlier in this batch", null); break; }
                                    if (HypoResolve(parentPath, created, relocated) == null)
                                    { addErr(idx, "unresolved_parent",
                                             "no parent node at '" + parentPath + "'" + NearestHint(parentPath, willCreate), null); break; }
                                }

                                // Mirror WriteWidgetInline's Item/Container guard:
                                // bare instances of abstract layout bases crash the
                                // render tree (found live 2026-07-25).
                                if (typeName == "Item" || typeName == "Container")
                                {
                                    addErr(idx, "not_renderable",
                                           "'" + typeName + "' is an abstract layout base, not a renderable "
                                           + "widget -- a bare instance crashes the render tree. Use 'Panel' "
                                           + "(invisible layout container; add a Rectangle child for a "
                                           + "background) or 'Rectangle' (a filled/bordered box) instead.", null);
                                    break;
                                }

                                // Attempt collection routing: resolve the screen node
                                // and check for matching placeholder collections.
                                // If the screen is a forward reference (not yet created)
                                // we skip routing and record the flat path.
                                string recordedPath = basePath;
                                var screenNode = string.IsNullOrEmpty(parentPath)
                                    ? null : ResolveNode(parentPath);
                                if (screenNode != null && !string.IsNullOrEmpty(typeName))
                                {
                                    var childClr = ResolveWidgetClrType(typeName);
                                    if (childClr != null)
                                    {
                                        var routes = MatchingPlaceholderColls(
                                            screenNode, childClr, readOnly: false);
                                        if (routes.Count > 1)
                                        {
                                            // Mirror WriteWidgetInline's ambiguous_container refusal.
                                            addErr(idx, "ambiguous_container",
                                                   "type '" + typeName + "' fits multiple collections on '" +
                                                   parentPath + "': " + string.Join(", ", routes) +
                                                   " - pass the collection sub-path explicitly (e.g. " +
                                                   parentPath + "/" + routes[0] + ")", null);
                                            break;
                                        }
                                        if (routes.Count == 0)
                                        {
                                            // Check whether a read-only collection would
                                            // have matched (mirror WriteWidgetInline).
                                            var roHits = MatchingPlaceholderColls(
                                                screenNode, childClr, readOnly: true);
                                            if (roHits.Count > 0)
                                            {
                                                addErr(idx, "read_only_collection",
                                                       "type '" + typeName + "' only fits runtime-managed " +
                                                       "(read-only) collection(s) on '" + parentPath + "': " +
                                                       string.Join(", ", roHits) +
                                                       " - these cannot be authored into", null);
                                                break;
                                            }
                                            // Zero matches, no ro hit: flat add (no warning).
                                        }
                                        else // routes.Count == 1
                                        {
                                            // Widget will be routed into the collection.
                                            recordedPath = parentPath.TrimEnd('/') + "/" + routes[0] + "/" + name;
                                            addWarn(idx, "routed_into_collection",
                                                    "create_widget will place '" + name + "' into collection '" +
                                                    routes[0] + "' on '" + parentPath +
                                                    "'; effective path will be '" + recordedPath + "'");
                                        }
                                    }
                                }

                                // Credit an earlier delete, same as other create ops.
                                if (ResolveNode(recordedPath) != null && !deleted.Contains(recordedPath))
                                    addWarn(idx, "already_exists",
                                            "'" + recordedPath + "' already exists in the live model; the create may collide");
                                created[recordedPath] = new HypoNode { Verb = verb, Type = typeName ?? "" };
                                deleted.Remove(recordedPath);
                            }
                            break;

                        default:
                            addWarn(idx, "unknown_op",
                                    "op '" + verb + "' is not validated by this bridge; it will be applied unchecked");
                            break;
                    }
                }

                if (strict && warnCount > 0)
                {
                    // strict: fold the warnings into errors so the batch refuses.
                    if (errCount > 0) errors.Append(",");
                    errors.Append(warnings);
                    errCount += warnCount;
                }

                bool ok = errCount == 0;
                return "{\"ok\":" + Bool(ok) + ",\"op_count\":" + opCount +
                       ",\"strict\":" + Bool(strict) +
                       ",\"errors\":[" + errors + "],\"warnings\":[" + warnings + "]}";
            }
        }
        catch (System.Text.Json.JsonException jx)
        {
            return ErrorJson("bad_json", "could not parse the ops body: " + jx.Message);
        }
        catch (Exception ex)
        {
            return ErrorJson("internal", ExcMsg(ex));
        }
    }

    // set_property / bind / attach_expression share one shape: they target a
    // property `name` on an existing-or-hypothetical node `path`.
    private void ValidateOnNode(
        int idx, string verb, string path, string name, string value,
        Dictionary<string, HypoNode> created, HashSet<string> deleted,
        Dictionary<string, string> relocated,
        List<string> willCreate,
        Action<int, string, string, string> addErr,
        Action<int, string, string> addWarn)
    {
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name))
        {
            addErr(idx, "bad_op", verb + " requires \"path\" and \"name\"", null);
            return;
        }
        // set_property DisplayName is the attribute route (SetDisplayNameInline),
        // valid on any node - it must not fall into the child-variable guards
        // below (which would refuse it as a node attribute). Deleted-node check
        // still applies; everything else about it needs no validation.
        if (verb == "set_property" && name == "DisplayName")
        {
            if (deleted.Contains(path))
                addErr(idx, "modifies_deleted_node",
                       verb + " targets '" + path + "', deleted earlier in this batch", null);
            return;
        }
        var fmtErr = FormatSpecifierError(name, value);
        if (fmtErr != null) { addErr(idx, "bad_value", fmtErr, null); return; }
        if (deleted.Contains(path))
        {
            addErr(idx, "modifies_deleted_node",
                   verb + " targets '" + path + "', deleted earlier in this batch", null);
            return;
        }

        // Resolve through the hypothetical tree (handles relocated prefix rewrites).
        string effectivePath = HypoResolve(path, created, relocated);
        var node = effectivePath != null ? ResolveNode(effectivePath) : null;
        if (node == null)
        {
            HypoNode hypoNode;
            if (effectivePath == null || !created.TryGetValue(effectivePath, out hypoNode))
            {
                addErr(idx, "unresolved_reference",
                       "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                return;
            }
            string hypoType = hypoNode.Type;
            // create_variable always produces an IUAVariable.  Setting
            // name="Value" is the variable self-assign path -- valid by
            // definition.  Return CLEAN; coercion is deferred to apply
            // (a live variable is required for type-checking).
            if (verb == "set_property" && name == "Value" &&
                hypoNode.Verb == "create_variable")
                return;
            // HYPOTHETICAL node: it does not exist yet, so the live-instance
            // guard cannot see it. Fall back to TYPE-level reflection off the
            // declared type - the same property set describe_type reports.
            if (string.IsNullOrEmpty(hypoType))
            {
                addWarn(idx, "unverifiable_property",
                        "'" + path + "' is created earlier in this batch without a declared "
                        + "type, so '" + name + "' cannot be checked until apply");
                return;
            }
            var clr = ResolveWidgetClrType(hypoType);
            if (clr == null)
            {
                addWarn(idx, "unverifiable_property",
                        "type '" + hypoType + "' does not resolve to a CLR type; '" + name +
                        "' cannot be checked until apply");
                return;
            }
            bool declared = clr.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                               .Where(IsLegendProp).Any(p => p.Name == name);
            if (!declared)
            {
                var valid = clr.GetProperties(BindingFlags.Public | BindingFlags.Instance)
                               .Where(IsLegendProp).Select(p => p.Name)
                               .GroupBy(x => x).Select(g => g.Key).OrderBy(x => x).ToList();
                var sugg = SuggestPropertyName(name, valid);
                var extra = new StringBuilder();
                extra.Append("\"valid_properties\":[");
                for (int i = 0; i < valid.Count; i++)
                {
                    if (i > 0) extra.Append(",");
                    extra.Append("\"" + JsonEscape(valid[i]) + "\"");
                }
                extra.Append("]");
                if (sugg != null)
                {
                    extra.Append(",\"did_you_mean\":\"" + JsonEscape(sugg) + "\"");
                }
                addErr(idx, "unknown_property",
                       hypoType + " has no settable property '" + name + "'" +
                       (sugg != null ? " (did you mean " + sugg + "?)" : ""), extra.ToString());
            }
            return;   // value coercion needs a live variable; deferred to apply
        }

        // Set a VARIABLE's OWN value: `set_property name=Value` on an IUAVariable
        // node targets the node itself, not a child variable -- mirror of
        // SetPropertyInline line 2771. Return here so DeclaredPropertyGuard never
        // sees "Value" as a property name on the variable. (An IUAObject with a
        // real child "Value" property, e.g. SpinBox, is not IUAVariable and falls
        // through to node.GetVariable("Value") + DeclaredPropertyGuard below.)
        if (verb == "set_property" && name == "Value" && node is IUAVariable selfVar)
        {
            if (IsArrayVariable(selfVar))
            {
                addErr(idx, "unsupported_array_write",
                       "variable '" + path + "' is array-typed; array writes are not supported via set_property",
                       null);
                return;
            }
            if (value != null)
            {
                var bad = CoerceCheck(selfVar, value);
                if (bad != null) addErr(idx, "bad_value", bad, null);
            }
            return;
        }

        // LIVE node: run the same guards the write path runs.
        var gate = DeclaredPropertyGuard(node, name);
        if (gate != null)
        {
            // DeclaredPropertyGuard hands back a whole {"error":{...}} object.
            // Unwrap its inner fields so the report stays one flat shape.
            addErr(idx, "unknown_property",
                   node.GetType().Name + " has no settable property '" + name + "'",
                   "\"guard\":" + gate);
            return;
        }
        var arrGate = DeclaredArrayGuard(node, name);
        if (arrGate != null)
        {
            // Also a whole {"error":{...}} object - carry it structurally rather
            // than escaping JSON into the message field.
            addErr(idx, "unsupported_array_write",
                   "property '" + name + "' on " + node.GetType().Name +
                   " is array-typed; array writes are not supported via set_property",
                   "\"guard\":" + arrGate);
            return;
        }
        if (verb == "set_property" && value != null)
        {
            var v = node.GetVariable(name);
            if (v != null)
            {
                var bad = CoerceCheck(v, value);
                if (bad != null) addErr(idx, "bad_value", bad, null);
            }
            // A declared-but-unmaterialized property has no IUAVariable yet, so
            // the value cannot be type-checked here; the write materializes it.
        }
    }

    // Best-effort "did you mean this path" for an unresolved reference, drawn from
    // every path the batch creates ANYWHERE in the list (see the pre-pass): the
    // usual cause is an op-ordering mistake, and naming the later create is what
    // makes the error fixable rather than just true.
    private static string NearestHint(string path, List<string> willCreate)
    {
        if (willCreate == null || willCreate.Count == 0) return "";
        // Exact match first: the path IS created, just too late.
        foreach (var c in willCreate)
        {
            if (string.Equals(c, path, StringComparison.OrdinalIgnoreCase))
                return " (this batch creates '" + c + "' at a LATER op - is the op order wrong?)";
        }
        var hit = SuggestPropertyName(path, willCreate);
        if (hit != null)
            return " (this batch creates '" + hit + "' - did you mean that, or is the op order wrong?)";
        return "";
    }

    // Same story for the TYPE field: create_widget says "widget_type",
    // create_object "object_type", create_type "base_type". A missed type here is
    // softer than a missed parent - the node is still tracked as created, but the
    // hypothetical property check downgrades to an `unverifiable_property`
    // warning instead of validating.
    private static string TypeKey(System.Text.Json.JsonElement op)
    {
        foreach (var key in new[] { "type", "widget_type", "object_type", "base_type" })
        {
            var v = JsonStr(op, key);
            if (!string.IsNullOrEmpty(v)) return v;
        }
        return null;
    }

    // Which op field names the PARENT depends on the noun, because each create
    // op mirrors its per-noun tool's own vocabulary: create_widget takes
    // "screen", create_folder/object/type/variable take "parent", create_alias
    // takes "parent_path". Checked in that order, with "path" last as a
    // fallback. Getting this wrong is silent: the validator computes the wrong
    // child path, so create-tracking misses and Tier 2 stops working while every
    // report still looks clean (caught by the live gate 2026-07-25).
    private static string ParentKey(System.Text.Json.JsonElement op)
    {
        foreach (var key in new[] { "parent", "screen", "parent_path", "path" })
        {
            var v = JsonStr(op, key);
            if (!string.IsNullOrEmpty(v)) return v;
        }
        return null;
    }

    // Read a string field off a JSON object, whatever its scalar kind.
    private static string JsonStr(System.Text.Json.JsonElement obj, string key)
    {
        System.Text.Json.JsonElement el;
        if (!obj.TryGetProperty(key, out el)) return null;
        switch (el.ValueKind)
        {
            case System.Text.Json.JsonValueKind.String: return el.GetString();
            case System.Text.Json.JsonValueKind.Number: return el.GetRawText();
            case System.Text.Json.JsonValueKind.True: return "true";
            case System.Text.Json.JsonValueKind.False: return "false";
            default: return null;
        }
    }

    // ---- U16: check-only twins of the coercion path -------------------------

    // The VALUE-VALIDITY half of CoerceAssign, with every `v.Value = ...` write
    // removed. Returns the same error strings CoerceAssign would, or null when
    // the value would assign cleanly.
    //
    // Kept as a separate method rather than a flag on CoerceAssign: the write
    // path is load-bearing and crash-adjacent (an invalid enum assign asserts
    // inside the native layer), so it is not worth threading a "don't write"
    // branch through it. The arms MUST stay in sync - if you add a datatype to
    // CoerceAssign, add it here.
    //
    // Arms with no validation to do (Boolean, String, LocalizedText) return null
    // because CoerceAssign accepts anything for them: Boolean maps any string to
    // a bool rather than rejecting, so a validator that "failed" on "yes" would
    // be lying about what the write would do.
    private string CoerceCheck(IUAVariable v, string raw)
    {
        string dt = DataTypeName(v);
        if (IsArrayVariable(v))
            return "unsupported_array_write: property is array-typed (" + dt +
                   "[]); scalar writes to array UA variables are not supported" +
                   " (they can crash Studio)";
        switch (dt)
        {
            case "Boolean":
            case "LocalizedText":
            case "String":
                return null;   // CoerceAssign accepts any string for these
            case "Int16":
            case "Int32":
            case "Int64":
            case "UInt16":
            case "UInt32":
            case "UInt64":
            case "Byte":
            case "SByte":
                {
                    UAValue iv;
                    return ParseInteger(dt, raw, out iv);
                }
            case "Float":
            case "Double":
            case "Size":
                {
                    double dv;
                    if (!double.TryParse(raw, System.Globalization.NumberStyles.Float,
                                         System.Globalization.CultureInfo.InvariantCulture, out dv))
                        return "value must be a number for " + dt + ": " + raw;
                    return null;
                }
            case "NodeId":
                if (IsNodeIdClearToken(raw)) return null;
                return ResolveNode(raw) == null
                    ? "NodeId value must be a resolvable node path: " + raw : null;
            case "Color":
                return CheckColor(raw);
            default:
                return CheckEnumOrRaw(dt, raw);
        }
    }

    // Tokens meaning "no target" for a NodeId/NodePointer property. Kept in
    // one place so the validator and the setter can never disagree about
    // what clears a pointer.
    private static bool IsNodeIdClearToken(string raw)
    {
        string s = (raw ?? "").Trim();
        return s.Length == 0 ||
               s.Equals("null", System.StringComparison.OrdinalIgnoreCase);
    }

    // Color parse-check, mirroring CoerceAssign's Color arm without the write.
    private static string CheckColor(string raw)
    {
        string s = (raw ?? "").Trim();
        uint argb;
        if (s.StartsWith("#"))
        {
            string hex = s.Substring(1);
            if (hex.Length == 6) hex = "FF" + hex;
            if (!uint.TryParse(hex, System.Globalization.NumberStyles.HexNumber,
                               System.Globalization.CultureInfo.InvariantCulture, out argb))
                return "Color must be #RRGGBB / #AARRGGBB hex or a UInt32 decimal: " + raw;
            return null;
        }
        return uint.TryParse(s, out argb)
            ? null : "Color must be #RRGGBB / #AARRGGBB hex or a UInt32 decimal: " + raw;
    }

    // Enum check mirroring SetEnumOrRaw, minus the assign. Deliberately PERMISSIVE
    // for a datatype we have no member list for: SetEnumOrRaw discovers those by
    // attempting the write and catching the native assert, which a validator must
    // not do (it would mutate). Reporting "valid" there and letting the real write
    // surface the error beats false-rejecting a value that would have worked.
    private static string CheckEnumOrRaw(string dt, string raw)
    {
        int ord;
        if (int.TryParse(raw, out ord)) return null;
        if (TryEnumOrdinal(dt, raw, out ord)) return null;
        var known = KnownEnumMembers(dt);
        if (known != null)
            return "invalid value '" + raw + "' for enum " + dt + "; valid: " + string.Join(", ", known);
        return null;
    }

    // FTOptix's ValueFormatter accepts .NET STANDARD numeric specifiers (F1, N2, G,
    // E2, P1, C, D, X) but REJECTS custom patterns (0.0, 0.00, #.##, #,##0.0): a
    // custom pattern is let through at author-time and only throws at RUNTIME
    // ("Unsupported number format: <p>"), so the author burns a restart+screenshot
    // cycle to discover it. A "Format" value with NO ASCII letter can ONLY be a
    // custom numeric pattern (standard specifiers start with a letter; DateTime
    // formats always contain H/m/s/y/d/M), so reject it up front with a did_you_mean
    // pointing at the standard equivalent (decimal count -> F<n>, or N<n> if grouped).
    private static string FormatSpecifierError(string propName, string raw)
    {
        if (propName != "Format" || string.IsNullOrEmpty(raw)) return null;
        foreach (char c in raw)
            if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')) return null;
        int dot = raw.LastIndexOf('.');
        int decimals = dot < 0 ? 0 : raw.Length - dot - 1;
        string sugg = (raw.IndexOf(',') >= 0 ? "N" : "F") + decimals;
        return "invalid Format '" + raw + "': FTOptix accepts .NET standard specifiers " +
               "(F1, N2, G, P1), not custom patterns like 0.0/#.##; did_you_mean: '" + sugg + "'";
    }

    // Read the POST body that follows the headers. The accept loop does ONE 4KB
    // read, which usually swallows the whole request - but an op batch can exceed
    // that, so continue reading until Content-Length bytes are in hand. Decoded as
    // UTF-8 (the header read is ASCII, which is fine for headers and wrong for a
    // body carrying non-ASCII property values).
    private static string ReadRequestBody(NetworkStream stream, byte[] first, int firstLen, string head)
    {
        try
        {
            int sep = head.IndexOf("\r\n\r\n", StringComparison.Ordinal);
            int headerLen = sep >= 0 ? sep + 4 : firstLen;
            int contentLength = 0;
            foreach (var line in head.Split('\n'))
            {
                var t = line.Trim();
                if (t.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                    int.TryParse(t.Substring("Content-Length:".Length).Trim(), out contentLength);
            }
            var ms = new System.IO.MemoryStream();
            int already = Math.Max(0, firstLen - headerLen);
            if (already > 0) ms.Write(first, headerLen, already);
            var tmp = new byte[8192];
            while (ms.Length < contentLength)
            {
                int got = stream.Read(tmp, 0, tmp.Length);
                if (got <= 0) break;   // client closed early
                ms.Write(tmp, 0, got);
            }
            return Encoding.UTF8.GetString(ms.ToArray());
        }
        catch (Exception)
        {
            return "";
        }
    }

    private string PropOkJson(string path, string name, string dt, string via, IUAVariable v)
    {
        return "{\"ok\":true,\"path\":\"" + JsonEscape(path) +
               "\",\"name\":\"" + JsonEscape(name) +
               "\",\"datatype\":\"" + JsonEscape(dt) +
               "\",\"via\":\"" + via + "\",\"value\":\"" + JsonEscape(ValueString(v)) +
               "\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
    }

    // Assign an enum/unknown-datatype property from its string form.
    // Enums are Int32-backed; a bare-string assign hits an internal LocalizedText path
    // that asserts on an empty localeId. Prefer the integer ordinal (raw is a number,
    // or a resolvable friendly member name for the built-in alignment enums); only
    // fall back to the string assign for a genuinely non-enum exotic datatype.
    // dt (browse-name) -> the actual FTOptix .NET enum Type, resolved by reflection
    // and cached (misses cached as null). The scan is over already-loaded assemblies,
    // so at model-write time the FTOptix.UI enums are present.
    private static readonly Dictionary<string, Type> _enumTypeCache =
        new Dictionary<string, Type>();

    private static Type ResolveEnumType(string dt)
    {
        if (string.IsNullOrEmpty(dt)) return null;
        lock (_enumTypeCache)
        {
            Type cached;
            if (_enumTypeCache.TryGetValue(dt, out cached)) return cached;
            // Optix names some enum DATATYPES with an "Enum" suffix (FontWeightEnum)
            // while the .NET enum TYPE is bare (FTOptix.UI.FontWeight); others match
            // exactly (VerticalAlignment). Accept the datatype name AND the
            // suffix-stripped form. Prefer an exact match if both a "FooEnum" and a
            // "Foo" enum exist.
            string stripped = dt.EndsWith("Enum", StringComparison.Ordinal) && dt.Length > 4
                ? dt.Substring(0, dt.Length - 4) : null;
            Type exact = null, alt = null;
            foreach (var a in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = a.GetTypes(); }
                catch (ReflectionTypeLoadException e) { types = e.Types.Where(x => x != null).ToArray(); }
                catch { continue; }
                foreach (var cand in types)
                {
                    if (cand == null || !cand.IsEnum) continue;
                    if (cand.Name == dt) { exact = cand; break; }
                    if (stripped != null && alt == null && cand.Name == stripped) alt = cand;
                }
                if (exact != null) break;
            }
            Type found = exact ?? alt;
            _enumTypeCache[dt] = found;
            return found;
        }
    }

    private static string SetEnumOrRaw(IUAVariable v, string dt, string raw)
    {
        int ord;
        if (int.TryParse(raw, out ord)) { v.Value = ord; return null; }
        // GENERIC: reflect the property's real enum type and parse the friendly
        // name from its own metadata. Handles every enum (FontWeight,
        // TextHorizontalAlignment, alignment, ...) correctly -- no hardcoded
        // per-enum map to maintain or get wrong. Case-insensitive. An invalid
        // name gets a valid-member list straight from the enum.
        var et = ResolveEnumType(dt);
        if (et != null)
        {
            try { v.Value = Convert.ToInt32(Enum.Parse(et, raw, true)); return null; }
            catch
            {
                return "invalid value '" + raw + "' for enum " + dt + "; valid: " +
                       string.Join(", ", Enum.GetNames(et));
            }
        }
        // Fallback for the built-in alignment enums if reflection can't resolve the
        // type (redundant while FTOptix.UI is loaded, kept as belt-and-suspenders).
        if (TryEnumOrdinal(dt, raw, out ord)) { v.Value = ord; return null; }
        var known = KnownEnumMembers(dt);
        if (known != null)
            return "invalid value '" + raw + "' for enum " + dt + "; valid: " + string.Join(", ", known);
        // I31 (2026-08-28, SpinBox.ValueChangeBehaviour=__probe__ - Studio process
        // GONE, no log line, no managed exception). The string assign below is the
        // crash site: when the DATATYPE's browse name matches no loaded enum type,
        // an invalid enum member fell through to a bare-string assign whose failure
        // is a NATIVE assert, which the catch below can never see. Resolve the enum
        // from the OWNER's declared CLR property instead - the same reflection
        // DeclaredArrayGuard/DeclaredPropertyGuard use, and the one enum source that
        // does not depend on the datatype NAME - and answer with the same bad_value +
        // valid-member list the other 21 enum properties already produce.
        var declared = DeclaredEnumType(v);
        if (declared != null)
        {
            try { v.Value = Convert.ToInt32(Enum.Parse(declared, raw, true)); return null; }
            catch
            {
                return "invalid value '" + raw + "' for enum " + declared.Name + "; valid: " +
                       string.Join(", ", Enum.GetNames(declared));
            }
        }
        // Genuinely non-enum datatype: attempt the string assign, catching the
        // native assert so we still return a clean message.
        try { v.Value = raw; return null; }
        catch (Exception ex)
        {
            return "could not assign '" + raw + "' to a property of type " + dt +
                   " (for an enum, pass an integer ordinal or a valid member name; " +
                   "call describe_type for the property): " + ex.Message;
        }
    }

    // Member names for the built-in enums we know (mirrors TryEnumOrdinal). Used to build
    // a helpful valid-list on an invalid enum value. Extend alongside TryEnumOrdinal.
    // Listed in TRUE ordinal order (see TryEnumOrdinal) so the valid-list an
    // invalid value shows isn't misleading about position.
    private static string[] KnownEnumMembers(string dt)
    {
        switch (dt)
        {
            case "HorizontalAlignment": return new[] { "Left", "Right", "Center", "Stretch" };
            case "VerticalAlignment": return new[] { "Top", "Bottom", "Center", "Stretch" };
            default: return null;
        }
    }

    // Friendly member name -> ordinal for the built-in FTOptix.UI alignment enums.
    // Case-insensitive. Extend here as more enum properties are exercised.
    //
    // CRITICAL / REGRESSION-CITE: FTOptix.UI.{Horizontal,Vertical}Alignment do NOT
    // use the WPF-standard {Left/Top=0, Center=1, Right/Bottom=2} order. Verified
    // by reflecting FTOptix.UI.Net.dll (2026-07-25):
    //   VerticalAlignment   Top=0  Bottom=1  Center=2  Stretch=3
    //   HorizontalAlignment Left=0 Right=1   Center=2  Stretch=3
    // i.e. the EXTREME member is 1 and Center is 2. An earlier map assumed the WPF
    // order, so "Bottom" set ordinal 2 (=Center) and rendered centered -- a live
    // build burned ~14 tool calls reverse-engineering this. Do NOT "fix" Center
    // back to 1; that reintroduces the swap. (Note the sibling Content*/Text*
    // alignment enums DO use the standard Center=1 order -- different enums.)
    private static bool TryEnumOrdinal(string dt, string name, out int ord)
    {
        ord = 0;
        string key = (dt ?? "") + "." + (name ?? "").Trim().ToLowerInvariant();
        switch (key)
        {
            case "HorizontalAlignment.left": ord = 0; return true;
            case "HorizontalAlignment.right": ord = 1; return true;
            case "HorizontalAlignment.center": ord = 2; return true;
            case "HorizontalAlignment.stretch": ord = 3; return true;
            case "VerticalAlignment.top": ord = 0; return true;
            case "VerticalAlignment.bottom": ord = 1; return true;
            case "VerticalAlignment.center": ord = 2; return true;
            case "VerticalAlignment.stretch": ord = 3; return true;
            default: return false;
        }
    }

    // ---- semantic authoring (bind / alias / i18n / delete) ------------------
    // All node-model ops (SetDynamicLink / SetAlias / AddTranslation / Delete) -
    // off-thread-safe (same class as MakeObject/Add/GetOrCreateVariable), so they
    // run inline on the HTTP thread with no marshaling. (The typed-property setter
    // and DelayedTask were both ruled out live: off-thread crash / design-time hang.)

    private string BindPropertyInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string name = QueryParam(firstLine, "name");
        string source = QueryParam(firstLine, "source");
        string raw = QueryParam(firstLine, "raw");
        string modeStr = QueryParam(firstLine, "mode") ?? "Read";
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name) ||
            (string.IsNullOrEmpty(source) && string.IsNullOrEmpty(raw)))
            return ErrorJson("bad_query", "required: path, name, and source=<resolvable path> OR raw=<literal NodePath>");
        if (!string.IsNullOrEmpty(source) && !string.IsNullOrEmpty(raw))
            return ErrorJson("bad_query", "pass source OR raw, not both");
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            IUAVariable srcVar = null;
            if (!string.IsNullOrEmpty(source))
            {
                srcVar = ResolveNode(source) as IUAVariable;
                if (srcVar == null) return ErrorJson("source_not_variable",
                    "source is not a variable: " + source +
                    " - binding THROUGH an alias ({Alias1}/Child or ../../Alias1/Child)" +
                    " is deliberately unresolvable at bind time; pass it as raw= instead");
            }
            IUAVariable propVar = node.GetVariable(name);
            if (propVar == null)
            {
                var gateErr = DeclaredPropertyGuard(node, name);
                if (gateErr != null) return gateErr;
                // Same pre-materialization array gate as set_property (an already-
                // materialized array prop can still be dynamic-linked; only the
                // fresh materialization of one is blocked).
                var arrGateErr = DeclaredArrayGuard(node, name);
                if (arrGateErr != null) return arrGateErr;
                if (node is IUAObject obj) propVar = obj.GetOrCreateVariable(name);
                else if (node is IUAObjectType objT) propVar = objT.GetOrCreateVariable(name);
            }
            if (propVar == null) return ErrorJson("property_not_found", "no property " + name + " on " + path);
            DynamicLinkMode mode;
            switch (modeStr)
            {
                case "Write": mode = DynamicLinkMode.Write; break;
                case "ReadWrite": mode = DynamicLinkMode.ReadWrite; break;
                default: mode = DynamicLinkMode.Read; break;
            }

            // CLEAR THE PRIOR BINDING FIRST - this is Studio's own
            // right-click -> "Remove binding" step, and skipping it is a real
            // bug: SetDynamicLink replaces the link's VALUE but leaves any
            // converter already attached to that link in place, so the
            // property ends up with a target AND a stale converter fighting
            // over it - reported as a clean success. Measured 2026-09-01:
            // binding a Label.Text that carried a formatted dynamic link left
            // its DynamicLinkFormatter attached. attach_formatter already
            // calls ResetDynamicLink; bind does too now. What was removed is
            // REPORTED rather than dropped silently.
            // ResetDynamicLink clears BOTH converter positions, so BOTH have
            // to be inspected before it runs:
            //   link-level     - HasConverter on the DynamicLink. Where a
            //                    StringFormatter (formatted dynamic link) sits.
            //   property-level - HasConverter on the property itself. Where an
            //                    ExpressionEvaluator sits (attach_expression
            //                    uses propVar.SetConverter, never the link).
            // Measured 2026-09-01: only the link was walked, so binding over a
            // property-level ExpressionEvaluator DESTROYED it and reported
            // "converter": false - an affirmative all-clear over a real loss,
            // which is worse than staying silent. Report the KIND too, so the
            // caller can tell a formatter from a formula it just lost.
            bool hadLink = false, hadConverter = false;
            string converterKind = "";
            try
            {
                var priorLink = propVar.Refs.GetVariable(
                    FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                if (priorLink != null)
                {
                    hadLink = true;
                    foreach (var c in priorLink.Children)
                        if (c is IUAObject)
                        {
                            hadConverter = true;
                            converterKind = c.BrowseName;
                            break;
                        }
                }
                var propConv = propVar.Refs.GetObject(
                    FTOptix.CoreBase.ReferenceTypes.HasConverter);
                if (propConv != null)
                {
                    hadConverter = true;
                    converterKind = propConv.BrowseName;
                }
            }
            catch { }
            propVar.ResetDynamicLink();
            string cleared = ",\"prior binding cleared\":{\"link\":" +
                (hadLink ? "true" : "false") + ",\"converter\":" +
                (hadConverter ? "true" : "false") +
                (hadConverter ? ",\"converter kind\":\"" +
                 JsonEscape(converterKind) + "\"" : "") + "}";

            if (srcVar != null)
            {
                propVar.SetDynamicLink(srcVar, mode);
                return "{\"ok\":true,\"path\":\"" + JsonEscape(path + "/" + name) +
                       "\",\"source\":\"" + JsonEscape(source) +
                       "\",\"mode\":\"" + JsonEscape(modeStr) + "\",\"via\":\"dynamiclink\""
                       + cleared + "}";
            }
            // RAW NodePath binding - the alias/template mechanism. The stored
            // value is a LITERAL path ("{Alias1}/MyInt" or "../../Alias1/MyInt")
            // resolved at RUNTIME per instance - deliberately NOT resolvable at
            // bind time (that per-instance late binding is what makes a template
            // reusable). Studio 1.7.x legacy pattern per the NetLogic cheatsheet:
            // materialize an empty link, then write the literal into the
            // DynamicLink variable (1.8.x gets SetDynamicLinkToAlias; not in
            // this SDK). No target validation is possible by design - the
            // response echoes raw for the caller to render-verify.
            propVar.SetDynamicLink(null, mode);
            var dlVar = propVar.Refs.GetVariable(FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
            if (dlVar == null)
                return ErrorJson("link_materialize_failed",
                    "SetDynamicLink(null) did not materialize a DynamicLink child on " + path + "/" + name);
            dlVar.Value = raw;
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path + "/" + name) +
                   "\",\"raw\":\"" + JsonEscape(raw) +
                   "\",\"mode\":\"" + JsonEscape(modeStr) + "\",\"via\":\"dynamiclink-raw\"" +
                   ",\"note\":\"literal NodePath - resolves per instance at runtime; render-verify\""
                   + cleared + "}";
        }
        catch (Exception ex) { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    private string CreateAliasInline(string firstLine)
    {
        string parent = QueryParam(firstLine, "parent");
        string name = QueryParam(firstLine, "name");
        string target = QueryParam(firstLine, "target");   // OPTIONAL: template aliases are unassigned by design
        string kind = QueryParam(firstLine, "kind");       // OPTIONAL: type constraint (Studio's "+ Alias" sets one)
        if (string.IsNullOrEmpty(parent) || string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "required: parent, name (+ optional target=<path>, kind=<type name or path>)");
        try
        {
            // Accept Object AND ObjectType parents - the primary home of an
            // alias slot is a TEMPLATE TYPE (create_type output), which is an
            // IUAObjectType; an IUAObject-only cast rejected exactly that
            // (found live 2026-07-17, same gap family as set_property-on-type).
            var parentNode = ResolveNode(parent);
            if (parentNode == null || !(parentNode is IUAObject || parentNode is IUAObjectType))
                return ErrorJson("node_not_found", "no object or type at parent: " + parent);
            IUANode targetNode = null;
            if (!string.IsNullOrEmpty(target))
            {
                targetNode = ResolveNode(target);
                if (targetNode == null) return ErrorJson("node_not_found", "no target at: " + target);
            }
            NodeId kindId = null;
            if (!string.IsNullOrEmpty(kind))
            {
                // Catalog UI type first (Button, ...), else a project path to a type node.
                var kf = typeof(FTOptix.UI.ObjectTypes).GetField(kind, BindingFlags.Public | BindingFlags.Static);
                if (kf != null && kf.GetValue(null) is NodeId knid) kindId = knid;
                if (kindId == null)
                {
                    var kn = ResolveNode(kind);
                    if (kn != null && (kn.NodeClass == NodeClass.ObjectType || kn.NodeClass == NodeClass.VariableType))
                        kindId = kn.NodeId;
                }
                if (kindId == null)
                    return ErrorJson("type_not_found",
                        "kind '" + kind + "' is neither a builtin UI type nor a path to a type node");
            }
            var dup = DupNameGuard(parentNode, name, parent);
            if (dup != null) return dup;
            // CREATE a new alias. IUAObject.SetAlias(name, target) only ASSIGNS a
            // target to an alias the node's TYPE already declares - it raises
            // "Alias {name} not found" on an arbitrary node (live-validated 0.8.2).
            // An Alias is a variable subtype (FTOptix.Core.Alias, YAML `Type: Alias`,
            // DataType NodeId, Value = target path). Build with node-model ops.
            // Kind (the type CONSTRAINT Studio's "+ Alias" carries - what makes
            // binding/validation "know" the alias's shape) is set via the typed
            // setter BEFORE parentNode.Add: a detached node isn't observed by the
            // renderer, so the off-thread typed-setter hazard doesn't apply yet.
            var alias = InformationModel.MakeVariable<FTOptix.Core.Alias>(name, OpcUa.DataTypes.NodeId);
            if (kindId != null) alias.Kind = kindId;
            if (targetNode != null) alias.Value = targetNode.NodeId;
            parentNode.Add(alias);
            return "{\"ok\":true,\"alias\":\"" + JsonEscape(parent + "/" + name) +
                   "\",\"target\":" + (targetNode != null ? "\"" + JsonEscape(target) + "\"" : "null") +
                   ",\"kind\":" + (kindId != null ? "\"" + JsonEscape(kind) + "\"" : "null") +
                   ",\"via\":\"alias-create\"}";
        }
        catch (Exception ex) { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    private string AddTranslationInline(string firstLine)
    {
        string key = QueryParam(firstLine, "key");
        string value = QueryParam(firstLine, "value") ?? "";
        string locale = QueryParam(firstLine, "locale") ?? "en-US";
        if (string.IsNullOrEmpty(key))
            return ErrorJson("bad_query", "required: key");
        try
        {
            int nsIdx = Project.Current.NodeId.NamespaceIndex;
            var existing = InformationModel.LookupTranslation(new LocalizedText(key));
            bool isNew = existing == null || string.IsNullOrEmpty(existing.Text);
            var lt = new LocalizedText(nsIdx, key, value, locale);
            if (isNew) InformationModel.AddTranslation(lt);
            else InformationModel.SetTranslation(lt);
            return "{\"ok\":true,\"key\":\"" + JsonEscape(key) + "\",\"locale\":\"" + JsonEscape(locale) +
                   "\",\"new\":" + Bool(isNew) + ",\"via\":\"translation\"}";
        }
        catch (Exception ex) { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    private string DeleteNodeInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        if (string.IsNullOrEmpty(path))
            return ErrorJson("bad_query", "required: path");
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            node.Delete();
            return "{\"ok\":true,\"deleted\":\"" + JsonEscape(path) + "\",\"via\":\"delete\"}";
        }
        catch (Exception ex) { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    // new method. Verified against the real installed SDK
    // (UAManagedCore.dll / UAManagedCoreCommon.dll via .NET reflection) that
    // IUAObject.ExecuteMethod(name, inArgs, out outArgs) is the correct API before
    // writing this, and compile-checked it in isolation since two of the project's
    // other referenced DLLs wouldn't load cleanly in an ad-hoc build context.
    // POST /bridge/node/invoke?path=<node>&method=<name>[&args=v1,v2,...] - execute an
    // exported UAMethod (an [ExportMethod] C# method, or a built-in library method such
    // as FTOptix's SearchBrokenDynamicLinks/FixAliasDynamicLinkMode) that isn't already
    // covered by a dedicated bridge verb. This is the generic escape hatch: right-click
    // "Execute" in Studio's UI does exactly this under the hood (IUAObject.ExecuteMethod),
    // so this exposes the same capability remotely instead of requiring a human at the
    // keyboard for every method Studio ships or a project author writes.
    //
    // args, if present, is a comma-separated list (matching this file's existing
    // convention for list-shaped query params, e.g. attach-expression's "sources") -
    // each element is passed through as a plain string. Methods that need typed,
    // numeric, or array arguments are out of scope for this generic endpoint; the
    // input arg marshaling here is intentionally minimal.
    //
    // Output arguments (if the method has any) come back as output_args, each
    // stringified best-effort - good enough to report a result/count/summary, not a
    // substitute for a typed read.
    //
    // Same trust model as every other write verb in this file (set-property,
    // delete-node, convert-to-type, ...): no allow-list, no confirmation step. This
    // runs at Studio DESIGN TIME against Project.Current, not a live runtime/PLC - the
    // blast radius is "the open project", the same as any other bridge write.
    //
    // SESSION, NOT THREAD (issue #4, root-caused 2026-09-24). ExecuteMethod on this
    // HTTP thread used to kill FTOptixStudio.exe on ANY method - Optix's own
    // SearchBrokenDynamicLinks, a custom method that throws mid-mutation (#4), even
    // a method whose body does nothing. Every kill was the same native heap
    // corruption (c0000374 in ntdll). The cause is that this thread has no Optix
    // session: native code dereferences the missing session (merely READING
    // Context.Sessions.CurrentSessionInfo here is enough to kill Studio). Studio's
    // right-click Execute survives because the GUI thread has one; the managed path
    // is otherwise identical (LogicBehavior.ExecuteMethod -> NetCodeExecutor is
    // synchronous reflection that catches and logs the method's exceptions).
    // ImpersonateRootTemporary() gives this thread a session for the call. Proven
    // live on the bare-vs-impersonated A/B: no-op, CheckFormula, the #4 shape and UI
    // creation all survive impersonated; the same calls bare kill Studio.
    // Dead ends, for the record: DelayedTask/LongRunningTask/IContext.Dispatch never
    // fire at design time, and Sessions.CurrentSessionHandler throws
    // NotImplementedException, so the GUI session cannot be borrowed - root it is.
    // The legacy unsafe=1 query param is accepted and ignored.
    private string InvokeMethodInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string methodName = QueryParam(firstLine, "method");
        if (string.IsNullOrEmpty(path))
            return ErrorJson("bad_query", "missing required query param: path");
        if (string.IsNullOrEmpty(methodName))
            return ErrorJson("bad_query", "missing required query param: method");

        var node = ResolveNode(path);
        if (node == null)
            return ErrorJson("node_not_found", "no node at path: " + path);

        var obj = node as IUAObject;
        if (obj == null)
            return ErrorJson("not_invokable",
                "node at " + path + " is a " + node.GetType().Name +
                " - only IUAObject nodes host invokable methods");

        string argsParam = QueryParam(firstLine, "args");
        object[] inputArgs = string.IsNullOrEmpty(argsParam)
            ? new object[0]
            : argsParam.Split(',').Select(s => (object)s).ToArray();

        object[] outputArgs;
        try
        {
            // NEVER call ExecuteMethod (or read session info) outside this scope.
            using (obj.Context.Sessions.ImpersonateRootTemporary())
                obj.ExecuteMethod(methodName, inputArgs, out outputArgs);
        }
        catch (Exception ex)
        {
            return ErrorJson("execution_failed", ExcMsg(ex));
        }

        var outSb = new StringBuilder();
        if (outputArgs != null)
        {
            for (int i = 0; i < outputArgs.Length; i++)
            {
                if (i > 0) outSb.Append(",");
                var v = outputArgs[i];
                outSb.Append("\"" + JsonEscape(v == null ? "null" : v.ToString()) + "\"");
            }
        }
        return "{\"ok\":true,\"path\":\"" + JsonEscape(path) +
               "\",\"method\":\"" + JsonEscape(methodName) +
               "\",\"session\":\"root\",\"output_args\":[" + outSb + "]}";
    }

    // Wire a UI event on a node (EventHandler graph, reverse-engineered from
    // FTRemoteAccessWidgetSetupLogic.cs + the NetLogic_CheatSheet). Node-model ops only.
    // TWO modes (use ONE):
    //   - NATIVE COMMAND (preferred - no custom NetLogic):
    //       command=SetVariable&variable=<path>&value=<v>   -> VariableCommands.Set
    //       command=ToggleVariable&variable=<path>          -> VariableCommands.Toggle
    //     ObjectPointer -> the builtin FTOptix.CoreBase.Objects.VariableCommands object;
    //     InputArguments = VariableToModify (VariablePointer) [+ Value] + ArrayIndex,
    //     the proven shape (FTRemoteAccessWidgetSetupLogic.cs:128-161 + cheatsheet).
    //   - NATIVE UI COMMAND (also no custom NetLogic - see _UiCommands):
    //       command=OpenDialog|CloseDialog|OpenKeyboard [&args=Name=Value;...]
    //     ObjectPointer -> the builtin UICommands object, which lives OUTSIDE the
    //     project root and so cannot be reached as a `method` path at all.
    //   - CUSTOM METHOD: method=ObjectPath/MethodName (an object owning an [ExportMethod]).
    // ObjectPointer is a typed FTOptix.Core.NodePointer (the dispatcher resolves the call
    // target through it; a plain NodeId wires but never invokes). FTOptix.Core/.CoreBase
    // are referenced, so the fully-qualified names add no module ref. EventHandler is
    // fully-qualified to dodge the System.EventHandler ambiguity (CS0104). Node-attach
    // order matches the sample: container -> ObjectPointer, Method, InputArguments; then
    // populate InputArguments after it's parented.
    // Native FTOptix.UI commands reachable BY NAME through `command`, the way the
    // VariableCommands pair is. They all live on the builtin UICommands object
    // OUTSIDE the project root, which is why they cannot be a `method` path (a
    // method_path of "Root/Objects/Commands/UICommands/OpenKeyboard" answers
    // node_not_found - the field detour that motivated this list).
    //   OpenDialog    Dialog (NodeId), AliasNode (NodeId), ParentItem (NodeId)
    //   CloseDialog   no arguments
    //   OpenKeyboard  KeyboardType (String), TargetVariable (NodeId),
    //                 ParentItem (NodeId)   <- Modules/FTOptix.UI/<ver>/Module.xml
    // KEEP IN SYNC with service/core.py::bridge_wire_event's _UI_COMMANDS.
    private static readonly string[] _UiCommands = { "OpenDialog", "CloseDialog", "OpenKeyboard" };

    // OpenKeyboard's three arguments, in Module.xml order. All three must be
    // present (an empty ParentItem is the late-bound form, not an absent one).
    private static readonly string[] _OpenKeyboardArgs =
        { "KeyboardType", "TargetVariable", "ParentItem" };

    // null when `args` names exactly OpenKeyboard's three arguments, else the
    // message saying which are missing or unrecognized. Count, not order: the
    // runtime counts arguments and rejects the call if the number is wrong.
    private static string OpenKeyboardArgsError(string argsParam)
    {
        var given = new List<string>();
        foreach (var pair in (argsParam ?? "").Split(';'))
        {
            int eq = pair.IndexOf('=');
            if (eq <= 0) continue;
            given.Add(pair.Substring(0, eq).Trim());
        }
        var missing = new List<string>();
        foreach (var need in _OpenKeyboardArgs)
            if (!given.Contains(need)) missing.Add(need);
        var extra = new List<string>();
        foreach (var g in given)
            if (Array.IndexOf(_OpenKeyboardArgs, g) < 0) extra.Add(g);
        if (missing.Count == 0 && extra.Count == 0) return null;
        return "OpenKeyboard takes exactly its three InputArguments" +
               (missing.Count > 0 ? " - missing: " + string.Join(", ", missing) : "") +
               (extra.Count > 0 ? " - not an OpenKeyboard argument: " + string.Join(", ", extra) : "") +
               ". Pass args=\"KeyboardType=<Numeric|AlphaNumeric|...>;TargetVariable=" +
               "<variable path>;ParentItem=\" - an empty ParentItem is late-bound to " +
               "the event node, as a Studio-authored handler does. A wrong argument " +
               "count fails at RUNTIME, silently, on the click.";
    }

    private string WireEventInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string evt = QueryParam(firstLine, "event");
        string command = QueryParam(firstLine, "command");
        string method = QueryParam(firstLine, "method");
        // B19: method ARGUMENTS. Without this only argument-less methods are
        // reachable (Dialog.Close, *.Refresh) -- and the ones that matter all take
        // arguments: ChangePanel(NewPanel), ChangePanelByTabName(TabName),
        // OpenDialog(Dialog, AliasNode). Semicolon-separated Name=Value pairs;
        // ';' rather than ',' because node paths routinely contain commas.
        string argsParam = QueryParam(firstLine, "args");
        // explicit caller escape hatch. When replace=true and a
        // matching handler exists, delete it first and proceed with the full
        // create path. The default (replace absent / false) remains in-place
        // update or the unsafe-error gate.
        string replaceRaw = QueryParam(firstLine, "replace");
        bool replace = string.Equals(replaceRaw, "true", StringComparison.OrdinalIgnoreCase);
        // B3-on-the-ObjectPointer. An ABSOLUTE NodeId names the DESIGN-TIME node,
        // but a UI object is instantiated PER SESSION under
        // UI/<engine>/Sessions/<id>/UIRoot/..., so at runtime the call target is
        // not at the pointed NodeId and the dispatcher answers "No behaviour or
        // observer found that handles the method". The command path escapes this
        // only because VariableCommands is a global singleton.
        // `object_raw` is a LITERAL NodePath RELATIVE TO THE EVENT NODE (the node
        // the handler hangs off) -- e.g. "../Loader" for a sibling. The four
        // levels from ObjectPointer up to that node (MethodContainer1 /
        // MethodsToCall / EventHandler) are prepended here so callers need not
        // count them. Method-path mode only; ignored with `command`.
        string objectRaw = QueryParam(firstLine, "object_raw");
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(evt))
            return ErrorJson("bad_query", "required: path, event");
        if (string.IsNullOrEmpty(command) && string.IsNullOrEmpty(method))
            return ErrorJson("bad_query", "required: command (SetVariable|ToggleVariable) or method (ObjectPath/MethodName)");
        try
        {
            var node = ResolveNode(path);
            if (node == null) return ErrorJson("node_not_found", "no node at: " + path);
            var evtTypeId = ResolveEventType(evt);
            if (evtTypeId == null)
            {
                // Mirror the property guard: reject-with-valid-list. Event names are NOT
                // derivable from describe (they're SDK identifiers), so a bare miss left
                // the model guessing ("Click" instead of "MouseClickEvent" - the A/B trap
                // that beat even describe-first arms). Hand back the authoritative set +
                // a best-effort suggestion.
                var valid = ValidUiEventNames();
                var suggestion = SuggestUiEvent(evt, valid);
                var sb = new StringBuilder();
                sb.Append("{\"error\":{\"code\":\"event_not_found\",\"message\":\"");
                sb.Append(JsonEscape("no builtin UI event type: " + evt +
                    (suggestion != null ? " (did you mean " + suggestion + "?)" : "") +
                    " - use one of valid_events"));
                sb.Append("\"");
                if (suggestion != null)
                {
                    sb.Append(",\"suggestion\":\""); sb.Append(JsonEscape(suggestion)); sb.Append("\"");
                }
                sb.Append(",\"valid_events\":[");
                for (int i = 0; i < valid.Count; i++)
                {
                    if (i > 0) sb.Append(",");
                    sb.Append("\""); sb.Append(JsonEscape(valid[i])); sb.Append("\"");
                }
                sb.Append("]}}");
                return sb.ToString();
            }

            // Resolve the call target (ObjectPointer value + Method name) by mode.
            NodeId objPtrTarget;
            string methodName;
            IUANode methodTargetNode = null;   // method mode only - the call target
            IUAVariable cmdTargetVar = null;   // command mode only
            string cmdValueRaw = null;         // command mode, Set only
            bool cmdNeedsValue = false;
            bool uiCommand = !string.IsNullOrEmpty(command) &&
                             Array.IndexOf(_UiCommands, command) >= 0;
            if (uiCommand)
            {
                // OpenKeyboard takes THREE InputArguments - KeyboardType (String),
                // TargetVariable (NodeId), ParentItem (NodeId), per
                // Modules/FTOptix.UI/<ver>/Module.xml. A wrong COUNT is a RUNTIME
                // error ("Called method OpenKeyboard with an invalid number of
                // arguments"), i.e. a click that does nothing and a Studio log line
                // nobody reads, so the count is settled here instead - before the
                // EventHandler is created, so a rejected call leaves no half-built
                // handler behind. Names, not order: the generic args block below
                // maps each pair to its argument by name.
                if (command == "OpenKeyboard")
                {
                    var argErr = OpenKeyboardArgsError(argsParam);
                    if (argErr != null) return ErrorJson("bad_query", argErr);
                }
                // Native UI commands (_UiCommands) live on the builtin FTOptix.UI
                // UICommands object at
                // /Objects/Commands/UICommands -- OUTSIDE the project root, so
                // ResolveNode cannot reach it and a method path of
                // "Commands/UICommands/OpenDialog" answers node_not_found. Fetch it
                // by PATH from the /Objects root (Project.Current.Owner), which is
                // how a Studio-authored handler points at it too
                // ("/Objects/Commands/UICommands"). Its arguments come from `args`
                // (Dialog=<type path>;AliasNode=<node>;ParentItem=) -- populated by
                // the generic args block below, where ParentItem, if named with an
                // EMPTY value, is late-bound to the event node ("..@NodeId" from
                // InputArguments), which is what a Studio-authored OpenDialog carries.
                IUANode uiObj = null;
                try { uiObj = Project.Current.Owner.Get("Commands/UICommands"); } catch { }
                if (uiObj == null) return ErrorJson("command_unavailable", "UICommands not in address space");
                objPtrTarget = uiObj.NodeId;
                methodName = command;
            }
            else if (!string.IsNullOrEmpty(command))
            {
                string varPath = QueryParam(firstLine, "variable");
                if (string.IsNullOrEmpty(varPath))
                    return ErrorJson("bad_query", "command mode requires: variable");
                cmdTargetVar = ResolveNode(varPath) as IUAVariable;
                if (cmdTargetVar == null) return ErrorJson("node_not_found", "no variable at: " + varPath);
                var vcObj = InformationModel.GetObject(FTOptix.CoreBase.Objects.VariableCommands);
                if (vcObj == null) return ErrorJson("command_unavailable", "VariableCommands not in address space");
                objPtrTarget = vcObj.NodeId;
                switch (command)
                {
                    case "SetVariable":
                    case "Set":
                        methodName = "Set"; cmdNeedsValue = true;
                        cmdValueRaw = QueryParam(firstLine, "value") ?? "";
                        break;
                    case "ToggleVariable":
                    case "Toggle":
                        methodName = "Toggle";
                        break;
                    default:
                        return ErrorJson("bad_query", "unknown command: " + command + " (SetVariable|ToggleVariable)");
                }
            }
            else
            {
                int slash = method.LastIndexOf('/');
                if (slash <= 0) return ErrorJson("bad_query", "method must be 'ObjectPath/MethodName'");
                var objNode = ResolveNode(method.Substring(0, slash));
                if (objNode == null) return ErrorJson("node_not_found", "no method object at: " + method.Substring(0, slash));
                objPtrTarget = objNode.NodeId;
                methodTargetNode = objNode;
                methodName = method.Substring(slash + 1);
            }

            // Existing-handler lookup: scan the node's children for any
            // FTOptix.CoreBase.EventHandler whose ListenEventType variable holds the
            // same NodeId as evtTypeId. Handlers authored in Studio can carry any
            // BrowseName, so the match is on event-type identity, NOT on the
            // "EH_" + evt + "_" + BrowseName string. The result gates all subsequent
            // behaviour: in-place update, unsafe-error
            //, or delete-then-create when replace=true.
            FTOptix.CoreBase.EventHandler existingHandler = null;
            foreach (var child in node.Children)
            {
                if (!(child is FTOptix.CoreBase.EventHandler candidateEh)) continue;
                var letCheck = candidateEh.GetVariable("ListenEventType");
                if (letCheck == null) continue;
                var candidateEvtId = letCheck.Value.Value as NodeId;
                if (candidateEvtId != null && candidateEvtId == evtTypeId)
                {
                    existingHandler = candidateEh;
                    break;
                }
            }

            // Gate: a matching handler already lives on the node.
            // replace=true  -> delete-then-create (explicit caller escape hatch).
            // replace=false -> in-place update (DEFAULT).
            bool replaced = false;
            if (existingHandler != null)
            {
                if (replace)
                {
                    // replace=true: delete the existing handler so the full normal
                    // create path below produces a clean, correctly-wired handler.
                    existingHandler.Delete();
                    replaced = true;
                }
                else
                {
                    // safety gate -- determine whether the found handler's
                    // shape allows a safe in-place rewrite before proceeding. Unsafe
                    // triggers (any one -> return handler_exists with existing summary):
                    //   (1) >1 MethodContainer in MethodsToCall -- ambiguous which to update.
                    //   (2) A sibling EventHandler on `node` whose ListenEventType cannot
                    //       be read -- we cannot confirm it is NOT also for this event type.
                    //   (3) A non-EventHandler child whose BrowseName equals the name we
                    //       would use for a freshly-created handler -- would block replace.
                    var mcList = new List<IUANode>();
                    foreach (var mcChild in existingHandler.MethodsToCall) mcList.Add(mcChild);

                    // Condition (1): count MethodContainers.
                    bool inPlaceSafe = mcList.Count == 1;

                    // Condition (2): scan siblings for EventHandlers with unreadable LET.
                    if (inPlaceSafe)
                    {
                        foreach (var sibling in node.Children)
                        {
                            if (!(sibling is FTOptix.CoreBase.EventHandler sibEh)) continue;
                            if (sibEh == existingHandler) continue;
                            var sibLet = sibEh.GetVariable("ListenEventType");
                            if (sibLet == null || !(sibLet.Value.Value is NodeId))
                            {
                                inPlaceSafe = false;
                                break;
                            }
                        }
                    }

                    // Condition (3): non-EventHandler child with the create-path BrowseName.
                    if (inPlaceSafe)
                    {
                        string expectedName = "EH_" + evt + "_" + node.BrowseName;
                        foreach (var c in node.Children)
                        {
                            if (c.BrowseName == expectedName &&
                                !(c is FTOptix.CoreBase.EventHandler))
                            {
                                inPlaceSafe = false;
                                break;
                            }
                        }
                    }

                    if (!inPlaceSafe)
                    {
                        // Build the existing-handler summary for the structured error.
                        // Summarise from the FIRST MethodContainer when one exists.
                        string existingBrowseName = existingHandler.BrowseName;
                        string existingTarget = "";
                        string existingArgsJson = "[]";

                        if (mcList.Count >= 1)
                        {
                            var firstMc = mcList[0];
                            var optrVar    = firstMc.GetVariable("ObjectPointer");
                            var mNameVar   = firstMc.GetVariable("Method");
                            string objStr  = optrVar?.Value.Value?.ToString() ?? "";
                            string mStr    = mNameVar?.Value.Value?.ToString() ?? "";
                            existingTarget = string.IsNullOrEmpty(mStr)
                                ? objStr
                                : (string.IsNullOrEmpty(objStr) ? mStr : objStr + "/" + mStr);

                            IUANode iaNode = null;
                            foreach (var ch in firstMc.Children)
                                if (ch.BrowseName == "InputArguments") { iaNode = ch; break; }
                            if (iaNode != null)
                            {
                                var names = iaNode.Children
                                    .Select(a => "\"" + JsonEscape(a.BrowseName) + "\"")
                                    .ToList();
                                existingArgsJson = "[" + string.Join(",", names) + "]";
                            }
                        }

                        var existSb = new StringBuilder();
                        existSb.Append("{\"ok\":false,\"error\":{\"code\":\"handler_exists\",\"message\":\"");
                        existSb.Append(JsonEscape(
                            "a handler for this event type already exists and cannot be safely "
                            + "rewritten in place: " + existingBrowseName
                            + " -- use replace=true to delete it first"));
                        existSb.Append("\",\"existing\":{\"browse_name\":\"");
                        existSb.Append(JsonEscape(existingBrowseName));
                        existSb.Append("\",\"target\":\"");
                        existSb.Append(JsonEscape(existingTarget));
                        existSb.Append("\",\"args\":");
                        existSb.Append(existingArgsJson);
                        existSb.Append("}}}");
                        return existSb.ToString();
                    }

                    // Safe path: exactly one MethodContainer, no
                    // ambiguous siblings, no name-conflict children -- proceed with
                    // in-place update -- rewrite MethodContainer1's ObjectPointer,
                    // Method, and InputArguments without deleting + recreating the
                    // handler. Arguments absent from the new call are DELETED
                    // (wholesale replacement of InputArguments), not left stale.
                    IUANode existingMc = null;
                    foreach (var ch in existingHandler.MethodsToCall)
                    { existingMc = ch; break; }
                    if (existingMc == null)
                        return ErrorJson("handler_malformed",
                            "existing handler has no MethodContainer in MethodsToCall: " +
                            existingHandler.BrowseName);

                    var existingObjPtr = existingMc.GetVariable("ObjectPointer");
                    if (existingObjPtr == null)
                        return ErrorJson("handler_malformed",
                            "existing handler MethodContainer has no ObjectPointer variable");
                    var existingMethodVar = existingMc.GetVariable("Method");
                    if (existingMethodVar == null)
                        return ErrorJson("handler_malformed",
                            "existing handler MethodContainer has no Method variable");

                    // Snapshot pre-update values for the changed list.
                    var prevObjPtrVal = existingObjPtr.Value.Value as NodeId;
                    var prevMethodName = existingMethodVar.Value.Value as string;
                    var prevLink = existingObjPtr.Refs.GetVariable(
                        FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                    string prevLinkPath = prevLink?.Value.Value as string;
                    var prevArgNames = new List<string>();
                    IUANode existingInputArgs = null;
                    foreach (var ch in existingMc.Children)
                        if (ch.BrowseName == "InputArguments") { existingInputArgs = ch; break; }
                    if (existingInputArgs != null)
                        foreach (var ch in existingInputArgs.Children)
                            prevArgNames.Add(ch.BrowseName);

                    // Compute rawPath for the update, using the existing ObjectPointer
                    // as the link holder (same SameTypeRelativePath call as create path).
                    bool ehAutoRel = false;
                    string ehRawPath = null;
                    if (!string.IsNullOrEmpty(objectRaw) && string.IsNullOrEmpty(command))
                        ehRawPath = "../../../../" + objectRaw;
                    else if (string.IsNullOrEmpty(command) && methodTargetNode != null)
                    {
                        ehRawPath = SameTypeRelativePath(existingObjPtr, methodTargetNode);
                        ehAutoRel = ehRawPath != null;
                    }

                    // --- Write ObjectPointer value ---
                    existingObjPtr.Value = objPtrTarget;

                    // --- Write link path ---
                    var opLink = existingObjPtr.Refs.GetVariable(
                        FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                    if (ehRawPath != null)
                    {
                        if (ehRawPath.IndexOf('@') < 0) ehRawPath += "@NodeId";
                        if (opLink == null)
                        {
                            // Materialize a new DynamicLink (same pattern as create path).
                            existingObjPtr.SetDynamicLink(null, DynamicLinkMode.Read);
                            opLink = existingObjPtr.Refs.GetVariable(
                                FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                            if (opLink == null)
                                return ErrorJson("link_materialize_failed",
                                    "SetDynamicLink(null) did not materialize a DynamicLink " +
                                    "on ObjectPointer (update path)");
                        }
                        opLink.Value = ehRawPath;
                        var kindVar = existingObjPtr.GetVariable("Kind");
                        if (kindVar != null) kindVar.Value = new NodeId(0, 58);
                    }
                    else if (opLink != null)
                    {
                        // No link wanted -- blank the path so the pointer falls back
                        // to the NodeId value rather than a stale relative path.
                        opLink.Value = "";
                    }

                    // --- Write Method name ---
                    existingMethodVar.Value = methodName;

                    // --- Replace InputArguments wholesale ---
                    // Delete all existing argument children first so no stale args remain.
                    if (existingInputArgs == null)
                    {
                        existingInputArgs = InformationModel.MakeObject("InputArguments");
                        existingMc.Add(existingInputArgs);
                    }
                    else
                    {
                        var toDelete = new List<IUANode>();
                        foreach (var ch in existingInputArgs.Children)
                            toDelete.Add(ch);
                        foreach (var ch in toDelete)
                            try { ch.Delete(); } catch { }
                    }
                    // Populate the now-empty InputArguments (same logic as create path).
                    if (cmdTargetVar != null)
                    {
                        var vtm = InformationModel.MakeVariable("VariableToModify",
                            FTOptix.Core.DataTypes.VariablePointer);
                        vtm.Value = cmdTargetVar.NodeId;
                        existingInputArgs.Add(vtm);
                        if (cmdNeedsValue)
                        {
                            var valVar = InformationModel.MakeVariable("Value",
                                cmdTargetVar.DataType);
                            existingInputArgs.Add(valVar);
                            var cverr = CoerceAssign(valVar, cmdValueRaw, firstLine);
                            if (cverr != null) return ErrorJson("bad_value", cverr);
                        }
                        var ai = InformationModel.MakeVariable("ArrayIndex",
                            OpcUa.DataTypes.UInt32);
                        ai.Value = (uint)0;
                        existingInputArgs.Add(ai);
                    }
                    else if (!string.IsNullOrEmpty(argsParam) || uiCommand)
                    {
                        foreach (var pair in (argsParam ?? "").Split(';'))
                        {
                            if (pair.Trim().Length == 0) continue;
                            int eq = pair.IndexOf('=');
                            if (eq <= 0)
                                return ErrorJson("bad_query",
                                    "args entry must be Name=Value, got: " + pair);
                            string argName = pair.Substring(0, eq).Trim();
                            string argVal  = pair.Substring(eq + 1);
                            if (uiCommand && argName == "ParentItem" && argVal.Length == 0)
                            {
                                var pv = InformationModel.MakeVariable(argName,
                                    OpcUa.DataTypes.NodeId);
                                existingInputArgs.Add(pv);
                                pv.SetDynamicLink(null, DynamicLinkMode.Read);
                                var pl = pv.Refs.GetVariable(
                                    FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                                if (pl != null) pl.Value = "../../../../..@NodeId";
                                continue;
                            }
                            if (argVal.Length == 0)
                            {
                                existingInputArgs.Add(InformationModel.MakeVariable(
                                    argName, OpcUa.DataTypes.NodeId));
                                continue;
                            }
                            var argTarget = ResolveNode(argVal);
                            if (argTarget != null)
                            {
                                var nv = InformationModel.MakeVariable(argName,
                                    OpcUa.DataTypes.NodeId);
                                nv.Value = argTarget.NodeId;
                                existingInputArgs.Add(nv);
                            }
                            else
                            {
                                var sv = InformationModel.MakeVariable(argName,
                                    OpcUa.DataTypes.String);
                                existingInputArgs.Add(sv);
                                var argErr = CoerceAssign(sv, argVal, firstLine);
                                if (argErr != null) return ErrorJson("bad_value", argErr);
                            }
                        }
                    }

                    // --- Read-back verification ---
                    var rbObjPtr = existingMc.GetVariable("ObjectPointer");
                    if (rbObjPtr == null)
                        return ErrorJson("readback_failed",
                            "ObjectPointer not readable after in-place update");
                    var rbObjPtrVal = rbObjPtr.Value.Value as NodeId;
                    if (rbObjPtrVal == null || rbObjPtrVal != objPtrTarget)
                        return ErrorJson("readback_mismatch",
                            "ObjectPointer value did not read back as written");

                    var rbMethodVar = existingMc.GetVariable("Method");
                    if (rbMethodVar == null)
                        return ErrorJson("readback_failed",
                            "Method not readable after in-place update");
                    var rbMethodName = rbMethodVar.Value.Value as string;
                    if (rbMethodName != methodName)
                        return ErrorJson("readback_mismatch",
                            "Method name did not read back as written: got " + rbMethodName);

                    // Link read-back: ehRawPath already has @NodeId appended if needed.
                    var rbLink = rbObjPtr.Refs.GetVariable(
                        FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                    string rbLinkPath = rbLink?.Value.Value as string;
                    if (ehRawPath != null && rbLinkPath != ehRawPath)
                        return ErrorJson("readback_mismatch",
                            "ObjectPointer link did not read back as written: " + rbLinkPath);

                    // InputArguments read-back: collect new arg names.
                    IUANode rbInputArgs = null;
                    foreach (var ch in existingMc.Children)
                        if (ch.BrowseName == "InputArguments") { rbInputArgs = ch; break; }
                    var newArgNames = new List<string>();
                    if (rbInputArgs != null)
                        foreach (var ch in rbInputArgs.Children)
                            newArgNames.Add(ch.BrowseName);

                    // --- Build changed list ---
                    var changed = new List<string>();
                    if (prevObjPtrVal != rbObjPtrVal) changed.Add("ObjectPointer");
                    if (prevLinkPath != rbLinkPath)   changed.Add("ObjectPointerLink");
                    if (prevMethodName != rbMethodName) changed.Add("Method");
                    bool argsDiffer = prevArgNames.Count != newArgNames.Count;
                    if (!argsDiffer)
                        for (int i = 0; i < prevArgNames.Count; i++)
                            if (prevArgNames[i] != newArgNames[i])
                            { argsDiffer = true; break; }
                    if (argsDiffer) changed.Add("InputArguments");

                    // Build the via string. Append +existing to reflect that the
                    // operation RESULT was an in-place update of an existing handler.
                    string updVia = (uiCommand ? "uicommand:" + methodName
                        : string.IsNullOrEmpty(command)
                        ? "eventhandler"
                          + (string.IsNullOrEmpty(argsParam) ? "" : "+args")
                          + (string.IsNullOrEmpty(objectRaw) ? "" : "+lateobj")
                          + (ehAutoRel ? "+reltype" : "")
                        : "command:" + methodName) + "+existing";

                    var changedSb = new StringBuilder("[");
                    for (int i = 0; i < changed.Count; i++)
                    {
                        if (i > 0) changedSb.Append(",");
                        changedSb.Append("\"");
                        changedSb.Append(JsonEscape(changed[i]));
                        changedSb.Append("\"");
                    }
                    changedSb.Append("]");

                    return "{\"ok\":true,\"updated\":true,\"node\":\"" + JsonEscape(path) +
                           "\",\"event\":\"" + JsonEscape(evt) +
                           "\",\"via\":\"" + JsonEscape(updVia) +
                           "\",\"changed\":" + changedSb + "}";
                }
            }

            var eh = InformationModel.MakeObject<FTOptix.CoreBase.EventHandler>("EH_" + evt + "_" + node.BrowseName);
            node.Add(eh);
            var letVar = eh.GetOrCreateVariable("ListenEventType");
            letVar.Value = evtTypeId;
            var mc = InformationModel.MakeObject("MethodContainer1");
            eh.MethodsToCall.Add(mc);
            var objPtr = InformationModel.MakeVariable<FTOptix.Core.NodePointer>(
                "ObjectPointer", OpcUa.DataTypes.NodeId);
            objPtr.Value = objPtrTarget;
            mc.Add(objPtr);
            // The link path the ObjectPointer should carry, if any. TWO sources:
            //   objectRaw   - the caller's own literal, relative to the event node
            //   same-type   - FIELD FINDING 2026-09-04. When the method object and
            //                 the event node live under the SAME ObjectType, the
            //                 absolute NodeId above names the TYPE's copy of the
            //                 object, so at runtime the per-session instance calls
            //                 into nothing: "No behaviour or observer found that
            //                 handles the method". Derive the relative path the way
            //                 bind gets it for free. Untouched across types, where
            //                 the absolute id is the correct target.
            bool autoRel = false;
            string rawPath = null;
            if (!string.IsNullOrEmpty(objectRaw) && string.IsNullOrEmpty(command))
                rawPath = "../../../../" + objectRaw;
            else if (string.IsNullOrEmpty(command) && methodTargetNode != null)
            {
                rawPath = SameTypeRelativePath(objPtr, methodTargetNode);
                autoRel = rawPath != null;
            }
            if (rawPath != null)
            {
                // Same legacy pattern as the raw bind: materialize an empty link,
                // then write the literal path into the DynamicLink variable. Must
                // happen AFTER mc.Add(objPtr) -- the link is a child reference and
                // needs the variable parented first.
                objPtr.SetDynamicLink(null, DynamicLinkMode.Read);
                var opLink = objPtr.Refs.GetVariable(FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                if (opLink == null)
                    return ErrorJson("link_materialize_failed",
                        "SetDynamicLink(null) did not materialize a DynamicLink on ObjectPointer");
                // The @ attribute selects what the link MEANS. A method target is
                // the NODE, so it needs @NodeId -- without it the link resolves to
                // the node's VALUE and the dispatcher gets nothing to call. A
                // Studio-authored handler stores "../../../../../Loader@NodeId";
                // the same path WITHOUT the suffix is accepted and silently does
                // nothing. Callers may supply their own @attr; only default it.
                if (rawPath.IndexOf('@') < 0) rawPath += "@NodeId";
                opLink.Value = rawPath;
                // Kind constrains what the pointer may address. Studio sets
                // BaseObjectType (ns=0;i=58) here; leaving it Null is the third
                // difference from a working handler.
                var kindVar = objPtr.GetVariable("Kind");
                if (kindVar != null)
                    kindVar.Value = new NodeId(0, 58);
            }
            var mName = InformationModel.MakeVariable("Method", OpcUa.DataTypes.String);
            mName.Value = methodName;
            mc.Add(mName);
            var inputArgs = InformationModel.MakeObject("InputArguments");
            mc.Add(inputArgs);

            if (cmdTargetVar != null)
            {
                // VariableToModify is a VariablePointer at the target variable's NodeId.
                var vtm = InformationModel.MakeVariable("VariableToModify", FTOptix.Core.DataTypes.VariablePointer);
                vtm.Value = cmdTargetVar.NodeId;
                inputArgs.Add(vtm);
                if (cmdNeedsValue)
                {
                    // Value typed to the target variable's own DataType. Route through
                    // CoerceAssign (the full set_property coercion) so enum / Color / NodeId
                    // targets resolve like set_property does. CoerceRaw's default arm
                    // bare-string-assigns them and hits the "!localeId.empty()" assert class.
                    var valVar = InformationModel.MakeVariable("Value", cmdTargetVar.DataType);
                    inputArgs.Add(valVar);
                    var cverr = CoerceAssign(valVar, cmdValueRaw, firstLine);
                    if (cverr != null) return ErrorJson("bad_value", cverr);
                }
                var ai = InformationModel.MakeVariable("ArrayIndex", OpcUa.DataTypes.UInt32);
                ai.Value = (uint)0;
                inputArgs.Add(ai);
            }
            else if (!string.IsNullOrEmpty(argsParam) || uiCommand)
            {
                // One typed child per Name=Value pair. The TYPE is decided by
                // whether the value resolves to a node, because that is the only
                // distinction the callers actually need:
                //
                //   ChangePanel(NewPanel=UI/Screens/Alarms)   -> a node
                //   ChangePanelByTabName(TabName=Alarms)      -> a string
                //
                // A node-valued argument is a NodePointer, NOT a bare NodeId, for
                // the same reason ObjectPointer above is one: the dispatcher
                // resolves the call target through the pointer, and a plain NodeId
                // wires but never resolves at invoke time.
                //
                // KNOWN LIMIT: an argument whose declared type is numeric
                // (ChangePanelByTabIndex(TabIndex)) lands as a String and will fail
                // at invoke. Prefer the ByTabName form. Typing these properly means
                // reading the method's declared InputArguments off the target type
                // rather than inferring, which is the right fix and a bigger one.
                foreach (var pair in (argsParam ?? "").Split(';'))
                {
                    if (pair.Trim().Length == 0) continue;
                    int eq = pair.IndexOf('=');
                    if (eq <= 0)
                        return ErrorJson("bad_query",
                            "args entry must be Name=Value, got: " + pair);
                    string argName = pair.Substring(0, eq).Trim();
                    string argVal = pair.Substring(eq + 1);
                    if (uiCommand && argName == "ParentItem" && argVal.Length == 0)
                    {
                        // The dialog's parent is the WIDGET THAT OPENED IT -- a
                        // per-session node, so it must be late-bound, never an
                        // absolute NodeId. Five levels up from a variable under
                        // InputArguments reaches the event node.
                        var pv = InformationModel.MakeVariable(argName, OpcUa.DataTypes.NodeId);
                        inputArgs.Add(pv);
                        pv.SetDynamicLink(null, DynamicLinkMode.Read);
                        var pl = pv.Refs.GetVariable(FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
                        if (pl != null) pl.Value = "../../../../..@NodeId";
                        continue;
                    }
                    // An EMPTY value declares the argument and leaves it Null.
                    // ChangePanel's signature is (NewPanel, AliasNode) and the
                    // OPTIONAL AliasNode must still be PRESENT -- a Studio-authored
                    // handler carries it as a Null variable, and omitting it is one
                    // of the three reasons a hand-built call silently does nothing.
                    if (argVal.Length == 0)
                    {
                        inputArgs.Add(InformationModel.MakeVariable(argName, OpcUa.DataTypes.NodeId));
                        continue;
                    }
                    var argTarget = ResolveNode(argVal);
                    if (argTarget != null)
                    {
                        // A PLAIN UAVariable holding the NodeId -- NOT a NodePointer.
                        // Verified against a Studio-authored ChangePanel handler:
                        // its NewPanel is a UAVariable, and a NodePointer there does
                        // not resolve (the call is accepted and does nothing).
                        // ObjectPointer is the one that IS a NodePointer.
                        var nv = InformationModel.MakeVariable(argName, OpcUa.DataTypes.NodeId);
                        nv.Value = argTarget.NodeId;
                        inputArgs.Add(nv);
                    }
                    else
                    {
                        var sv = InformationModel.MakeVariable(argName, OpcUa.DataTypes.String);
                        inputArgs.Add(sv);
                        var argErr = CoerceAssign(sv, argVal, firstLine);
                        if (argErr != null) return ErrorJson("bad_value", argErr);
                    }
                }
            }

            // Append +replaced when the RESULT was a delete-then-create (replace=true),
            // so the via string reflects the operation outcome, not just the request shape.
            string via = (uiCommand ? "uicommand:" + methodName
                : string.IsNullOrEmpty(command)
                ? "eventhandler"
                  + (string.IsNullOrEmpty(argsParam) ? "" : "+args")
                  + (string.IsNullOrEmpty(objectRaw) ? "" : "+lateobj")
                  + (autoRel ? "+reltype" : "")
                : "command:" + methodName)
                + (replaced ? "+replaced" : "");
            return "{\"ok\":true,\"node\":\"" + JsonEscape(path) + "\",\"event\":\"" + JsonEscape(evt) +
                   "\",\"via\":\"" + JsonEscape(via) + "\""
                   + (replaced ? ",\"replaced\":true" : "")
                   + "}";
        }
        catch (Exception ex) { return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}"; }
    }

    // ---- setup / project scaffolding ----------------------------------------

    // POST /bridge/setup/web-engine[?port=8081&ip=0.0.0.0]
    // Ensure a Web presentation engine exists under UI so the deployed runtime
    // serves a canvas (the manual "add UI -> Web presentation engine" step from
    // fresh-box validation). Idempotent: returns existed:true if one is already
    // present; otherwise creates + configures one named "WebPresentationEngine"
    // and points StartWindow at the first window in the project.
    private string EnsureWebEngineInline(string firstLine)
    {
        int port = WebEnginePort, parsed;
        if (int.TryParse(QueryParam(firstLine, "port"), out parsed) && parsed > 0) port = parsed;
        return EnsureWebEngineCore(port, QueryParam(firstLine, "ip") ?? "0.0.0.0");
    }

    // Shared by the HTTP endpoint and the [ExportMethod] SetupProject. Returns a JSON
    // status string. Off-thread-safe node-model ops only (works from the HTTP thread
    // AND design-time Studio).
    private string EnsureWebEngineCore(int port, string ip)
    {
        try
        {
            var ui = ResolveNode("UI");
            if (ui == null)
                return ErrorJson("node_not_found", "no UI node in project");

            // 1. Already present? (idempotent - one engine is enough)
            foreach (var existing in ui.Children)
                if (existing.GetType().Name == "WebUIPresentationEngine")
                    return "{\"ok\":true,\"existed\":true,\"path\":\"UI/" +
                           JsonEscape(existing.BrowseName) + "\"}";

            // 2. Resolve the type NodeId WITHOUT a compile-time dependency on the
            //    WebUI module: string-based Type.GetType returns null (graceful) on a
            //    wrong name rather than failing the compile, and the module IS loaded
            //    at runtime inside Studio. Mirrors the widget path's ObjectTypes-NodeId
            //    approach (typeof(FTOptix.UI.ObjectTypes) in WriteWidgetInline).
            NodeId engType = null;
            var otType = Type.GetType("FTOptix.WebUI.ObjectTypes, FTOptix.WebUI.Net");
            if (otType != null)
                foreach (var fieldName in new[] { "WebPresentationEngine", "WebUIPresentationEngine" })
                {
                    var f = otType.GetField(fieldName, BindingFlags.Public | BindingFlags.Static);
                    if (f != null && f.GetValue(null) is NodeId nid) { engType = nid; break; }
                }
            if (engType == null)
                return ErrorJson("type_unresolved",
                    "could not resolve the WebPresentationEngine type NodeId from FTOptix.WebUI.ObjectTypes");

            // 3. Create + add (node-model, off-thread-safe), THEN configure - inherited
            //    type properties materialize once the object is in the tree (same
            //    GetVariable-null-on-a-fresh-instance trap the property setter handles).
            var eng = InformationModel.MakeObject("WebPresentationEngine", engType);
            ui.Children.Add(eng);

            SetIfPresent(eng, "Port", port);        // UInt16 var accepts Int32 (see SetPropertyInline)
            SetIfPresent(eng, "IPAddress", ip);
            SetIfPresent(eng, "Protocol", 0);       // 0 = HTTP
            // MaxNumberOfConnections: a fresh MakeObject leaves this ABSENT (-> 0 -> the
            // deployed runtime refuses browser connections; the "not accessible" symptom).
            // Studio's own "Add Web presentation engine" sets 5 - match it so it serves.
            SetIfPresent(eng, "MaxNumberOfConnections", WebEngineMaxConnections);

            // StyleSheet -> the project DefaultStyleSheet so the canvas renders styled
            // (a fresh engine leaves it Null -> unstyled). Optional; skip if absent.
            var styleSheet = ResolveNode("UI/DefaultStyleSheet");
            if (styleSheet != null) SetIfPresent(eng, "StyleSheet", styleSheet.NodeId);

            // StartingUser -> the built-in Anonymous user at /Objects/Users/Anonymous, a
            // SYSTEM node OUTSIDE Project.Current (that's why a fresh project's own
            // Security/Users is empty). Without a session user the runtime won't serve.
            // Resolve via the Objects root = Project.Current's Owner.
            bool anon = false;
            var anonUser = ResolveAnonymousUser();
            if (anonUser != null) { SetIfPresent(eng, "StartingUser", anonUser.NodeId); anon = true; }

            // AllowedLocalSources -> Studio's default asset allow-list (empty on a fresh
            // engine -> the runtime blocks images/fonts/css/js).
            SetArrayIfPresent(eng, "AllowedLocalSources", WebEngineAllowedSources);

            // StartWindow -> the first window in the project, else leave unset.
            string startWinName = "";
            var startWin = FindFirstWindow(ui);
            if (startWin != null)
            {
                SetIfPresent(eng, "StartWindow", startWin.NodeId);
                startWinName = startWin.BrowseName;
            }

            return "{\"ok\":true,\"existed\":false,\"path\":\"UI/WebPresentationEngine\",\"port\":" +
                   port + ",\"protocol\":\"HTTP\",\"max_connections\":" + WebEngineMaxConnections +
                   ",\"styled\":" + Bool(styleSheet != null) + ",\"anonymous_user\":" + Bool(anon) +
                   ",\"allowed_sources\":" + WebEngineAllowedSources.Length +
                   ",\"start_window\":\"" + JsonEscape(startWinName) + "\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // The built-in Anonymous user (/Objects/Users/Anonymous). Project.Current is
    // /Objects/<project>, so its Owner is the Objects root; Get("Users/Anonymous")
    // from there reaches the system user. Returns null (graceful) if unresolvable.
    private IUANode ResolveAnonymousUser()
    {
        try
        {
            var objectsRoot = Project.Current.Owner;
            if (objectsRoot != null) return objectsRoot.Get("Users/Anonymous");
        }
        catch { /* leave StartingUser unset */ }
        return null;
    }

    // Assign a String[] to a materialized-or-materializable array property.
    private static void SetArrayIfPresent(IUANode node, string name, string[] values)
    {
        var v = node.GetVariable(name);
        if (v == null && node is IUAObject obj) v = obj.GetOrCreateVariable(name);
        if (v != null) v.Value = new UAValue(values);
    }

    // Assign a value to a materialized-or-materializable property (node-model). The
    // param is UAValue (not object) so each call site's concrete type (int/string/
    // NodeId) implicitly converts - an `object` needs an explicit (UAValue) cast at
    // the assignment site (CS0266); same reason CoerceRaw returns UAValue.
    private static void SetIfPresent(IUANode node, string name, UAValue value)
    {
        var v = node.GetVariable(name);
        if (v == null && node is IUAObject obj) v = obj.GetOrCreateVariable(name);
        if (v != null) v.Value = value;
    }

    // First window in the project (a StartWindow candidate). MainWindow is a direct
    // WindowType child of UI; also look one level into folders (e.g. UI/Screens).
    private IUANode FindFirstWindow(IUANode ui)
    {
        foreach (var c in ui.Children)
            if (c.GetType().Name == "WindowType") return c;
        foreach (var c in ui.Children)
            if (c.GetType().Name == "Folder")
                foreach (var g in c.Children)
                    if (Array.IndexOf(ScreenTypes, g.GetType().Name) >= 0) return g;
        return null;
    }

    // Parse an integer at the variable's declared width; null on success, else a
    // bad_value message. Range errors are refused, never wrapped.
    private static string ParseInteger(string dt, string raw, out UAValue val)
    {
        val = null;
        var ns = System.Globalization.NumberStyles.Integer;
        var ci = System.Globalization.CultureInfo.InvariantCulture;
        string s = (raw ?? "").Trim();
        if (dt == "UInt64")
        {
            ulong u;
            if (ulong.TryParse(s, ns, ci, out u)) { val = u; return null; }
            long neg;
            return long.TryParse(s, ns, ci, out neg)
                ? "value out of range for UInt64: " + raw
                : "value must be an integer for UInt64: " + raw;
        }
        long iv;
        if (!long.TryParse(s, ns, ci, out iv))
        {
            return System.Text.RegularExpressions.Regex.IsMatch(s, @"^[+-]?[0-9]+$")
                ? "value out of range for " + dt + ": " + raw
                : "value must be an integer for " + dt + ": " + raw;
        }
        switch (dt)
        {
            case "SByte":  if (iv >= sbyte.MinValue && iv <= sbyte.MaxValue) { val = (sbyte)iv; return null; } break;
            case "Byte":   if (iv >= byte.MinValue && iv <= byte.MaxValue) { val = (byte)iv; return null; } break;
            case "Int16":  if (iv >= short.MinValue && iv <= short.MaxValue) { val = (short)iv; return null; } break;
            case "UInt16": if (iv >= ushort.MinValue && iv <= ushort.MaxValue) { val = (ushort)iv; return null; } break;
            case "Int32":  if (iv >= int.MinValue && iv <= int.MaxValue) { val = (int)iv; return null; } break;
            case "UInt32": if (iv >= uint.MinValue && iv <= uint.MaxValue) { val = (uint)iv; return null; } break;
            case "Int64":  val = iv; return null;
        }
        return "value out of range for " + dt + ": " + raw;
    }

    // Coerce a query-string value to a target variable's DataType (mirror of the
    // SetPropertyInline switch; kept separate so that validated path is untouched).
    // Returns UAValue (not object) so each typed return implicitly converts - an
    // `object` would need an explicit (UAValue) cast at the assignment site (CS0266).
    private UAValue CoerceRaw(string dtName, string raw, string firstLine)
    {
        switch (dtName)
        {
            case "Boolean": return (raw == "true" || raw == "1" || raw == "True");
            case "Int16":
            case "Int32":
            case "Int64":
            case "UInt16":
            case "UInt32":
            case "UInt64":
            case "Byte":
            case "SByte":
                {
                    UAValue iv;
                    string err = ParseInteger(dtName, raw, out iv);
                    if (err != null) throw new FormatException(err);
                    return iv;
                }
            case "Float":
            case "Double":
            case "Size":
                return Convert.ToDouble(raw);
            case "LocalizedText":
                return new LocalizedText(raw, QueryParam(firstLine, "locale") ?? "en-US");
            default:
                return raw;
        }
    }

    // The 17 ExpressionEvaluator formula functions (see docs/expression-evaluator-
    // reference.md). Used by ValidateExpressionSyntax to flag an unknown call.
    private static readonly System.Collections.Generic.HashSet<string> ExprFunctions =
        new System.Collections.Generic.HashSet<string>(System.StringComparer.OrdinalIgnoreCase)
        { "max","min","avg","abs","trunc","ceil","floor","round","sqrt","sign","like",
          "isempty","if","left_of","right_of" };

    // LiteralLintTable -- maps runtime-rejected literal tokens to their design-time fixes.
    // Three literal classes are checked by ValidateExpressionSyntax without reimplementing
    // the ExpressionEvaluator grammar:
    //   Class 1 -- '#RRGGBB' / '#AARRGGBB' hex colour tokens: detected by pattern (6 or 8
    //             hex digits after a bare '#' outside a string literal); the runtime only
    //             accepts the 0xAARRGGBB uint form (e.g. '#FFF809' -> '0xFFFFF809').
    //   Class 2 -- Capitalised Boolean tokens: table below (LiteralBoolFixes); exact
    //             case-sensitive word-boundary match (True/False -> true/false).
    //   Class 3 -- Bare NodeId-typed source ref as a top-level result with no enclosing
    //             comparison or isempty() call: text-only detection is ambiguous without
    //             type information; check dropped -- validate in model-client tooling or
    //             code review instead.
    private static readonly System.Collections.Generic.Dictionary<string, string> LiteralBoolFixes =
        new System.Collections.Generic.Dictionary<string, string>
        { { "True", "true" }, { "False", "false" } };

    private const string ConcatErr =
        "ExpressionEvaluator '+' is numeric-only -- it can't compose a number with text " +
        "(e.g. round(...) + \" L\" silently no-ops, even fully parenthesized). To append a " +
        "unit/label, wrap the expression in a StringFormatter node with Format like " +
        "\"{0} L\" -- not '+ \"...\"'.";

    // FTOptix's ExpressionEvaluator '+' is NUMERIC-ONLY -- it cannot compose a number
    // with text (confirmed live 2026-07-26). Text composition needs a StringFormatter
    // node. Flag an arithmetic operator (+ - * /) adjacent (whitespace-skipped) to a
    // string literal. Low false-positive: if(c,"A","B") / left_of(x,"-") keep their
    // string args after a ',', not an arithmetic op, so they pass unflagged.
    private static string CheckNumStringConcat(string expr)
    {
        bool inStr = false; char prevSig = '\0';
        for (int i = 0; i < expr.Length; i++)
        {
            char c = expr[i];
            if (inStr) { if (c == '"') { inStr = false; prevSig = '"'; } continue; }
            if (char.IsWhiteSpace(c)) continue;
            if (c == '"')
            {
                if (prevSig == '+' || prevSig == '-' || prevSig == '*' || prevSig == '/')
                    return ConcatErr;
                inStr = true; prevSig = '"'; continue;
            }
            if ((c == '+' || c == '-' || c == '*' || c == '/') && prevSig == '"')
                return ConcatErr;
            prevSig = c;
        }
        return null;
    }

    // Structural validation of an ExpressionEvaluator formula.
    //
    // SDK PARSE-API PROBE -- re-investigated 2026-09-05.
    // Probe method: GET /bridge/diag/clrtype?name=FTOptix.CoreBase.ExpressionEvaluator
    // (DiagClrTypeJson, :5720) against the running Studio process, which walks
    // AppDomain.CurrentDomain.GetAssemblies() and reflects all public instance/static
    // methods + properties up the inheritance chain to NodeLogic/UAObject.
    //
    // Definitive result for FTOptix SDK <= 1.7.x (Optix 1.7.4.32 probe):
    //   Declared on ExpressionEvaluator: Expression (String, r/w),
    //                                    ExpressionVariable (String, r/w)
    //   Inherited (NodeLogic):           Start(), Stop()
    //   No Parse, TryParse, Validate, GetError, SyntaxCheck, or any other
    //   error-reading surface of any kind.
    //
    // CONCLUSION: Optix exposes NO design-time parser for ExpressionEvaluator.
    // Formulas are validated ONLY at RUNTIME (a bad one silently no-ops).
    // This literal-lint function is therefore the ONLY viable design-time check.
    // To re-probe a future SDK: arm the bridge and call DiagClrTypeJson(:5720).
    // If a parse API appears there, replace this function with the exact call site.
    //
    // What this catches WITHOUT reimplementing the grammar:
    //   * unbalanced ()/{}, out-of-range {N} placeholders, unknown function names,
    //     unterminated string literals, numeric+string concat via '+'
    //   * runtime-rejected literals (LiteralLintTable -- LiteralBoolFixes dict + hex pattern):
    //       Class 1 -- '#RRGGBB'/'#AARRGGBB' colour tokens -> 0xAARRGGBB uint literal
    //       Class 2 -- capitalised Boolean tokens: True/False -> true/false (LiteralBoolFixes)
    //       Class 3 -- bare NodeId source ref at top level: check dropped (ambiguous from
    //                 text alone; see LiteralLintTable comment above)
    // String literal state is tracked throughout so inner parens/braces/hash-tokens
    // inside quoted strings don't false-positive the checks above.
    // Returns null when the formula is structurally sound, else a human-readable reason.
    private static string ValidateExpressionSyntax(string expr, int sourceCount)
    {
        if (string.IsNullOrWhiteSpace(expr)) return "expression is empty";
        var concat = CheckNumStringConcat(expr);
        if (concat != null) return concat;
        int paren = 0;
        bool inStr = false;
        var word = new StringBuilder();
        for (int i = 0; i < expr.Length; i++)
        {
            char c = expr[i];
            if (inStr) { if (c == '"') inStr = false; continue; }
            if (c == '"')
            {
                // Class 2: check accumulated word before entering a string literal.
                var boolE = LiteralBoolErr(word.ToString()); if (boolE != null) return boolE;
                inStr = true; word.Clear(); continue;
            }
            if (c == '(')
            {
                // Class 2: check before function-name check so 'True(' gives the
                // more actionable "invalid_literal" message rather than "unknown function".
                var boolE = LiteralBoolErr(word.ToString()); if (boolE != null) return boolE;
                string w = word.ToString();
                if (w.Length > 0 && char.IsLetter(w[0]) && !ExprFunctions.Contains(w))
                    return "unknown function '" + w + "' (valid: " + string.Join(", ", ExprFunctions) + ")";
                paren++; word.Clear(); continue;
            }
            if (c == ')')
            {
                paren--;
                if (paren < 0) return "unbalanced parentheses: ')' with no matching '('";
                // Class 2: check accumulated word at closing paren boundary.
                var boolE = LiteralBoolErr(word.ToString()); if (boolE != null) return boolE;
                word.Clear(); continue;
            }
            if (c == '}') return "unbalanced braces: '}' with no matching '{'";
            if (c == '{')
            {
                // Class 2: check accumulated word before entering a brace group.
                var boolE = LiteralBoolErr(word.ToString()); if (boolE != null) return boolE;
                int j = expr.IndexOf('}', i);
                if (j < 0) return "unbalanced braces: '{' with no matching '}'";
                string inner = expr.Substring(i + 1, j - i - 1).Trim();
                if (inner.Length > 0 && inner[0] != '#')   // numeric source placeholder
                {
                    int idx;
                    if (!int.TryParse(inner, out idx))
                        return "invalid placeholder '{" + inner + "}' (use {0},{1},... or {#name})";
                    if (idx < 0 || idx >= sourceCount)
                        return "placeholder {" + idx + "} but only " + sourceCount + " source(s) provided";
                }
                i = j; word.Clear(); continue;
            }
            // Class 1: '#RRGGBB' or '#AARRGGBB' colour literal outside a string.
            // The runtime requires a 0xAARRGGBB uint; a CSS-style '#...' token silently
            // no-ops. {#name} placeholders are jumped over above, so '#' here is bare.
            if (c == '#')
            {
                int hexEnd = i + 1;
                while (hexEnd < expr.Length && IsHexDigit(expr[hexEnd])) hexEnd++;
                int hexLen = hexEnd - (i + 1);
                if (hexLen == 6 || hexLen == 8)
                {
                    string hex = expr.Substring(i + 1, hexLen);
                    string fix = (hexLen == 6 ? "0xFF" : "0x") + hex.ToUpper();
                    return "invalid_literal: '#" + hex + "' is a CSS hex colour -- "
                           + "ExpressionEvaluator expects a uint: use " + fix;
                }
                word.Clear(); continue;
            }
            if (char.IsLetterOrDigit(c) || c == '_') word.Append(c);
            else
            {
                // Class 2: check accumulated word at any other non-word boundary.
                var boolE = LiteralBoolErr(word.ToString()); if (boolE != null) return boolE;
                word.Clear();
            }
        }
        if (inStr) return "unterminated string literal";
        if (paren > 0) return "unbalanced parentheses: " + paren + " unclosed '('";
        // Class 2: check any word that ends exactly at the end of the expression.
        { var boolE = LiteralBoolErr(word.ToString()); if (boolE != null) return boolE; }
        return null;
    }

    // Class 1 (LiteralLintTable) helper: true iff c is a valid hexadecimal digit.
    private static bool IsHexDigit(char c)
    {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    }

    // Class 2 (LiteralLintTable) helper: return an invalid_literal error if w is a
    // capitalised Boolean token (True/False), otherwise null. Called at every word
    // boundary inside ValidateExpressionSyntax so that standalone True/False tokens
    // anywhere in the formula are caught before the expression reaches the runtime.
    private static string LiteralBoolErr(string w)
    {
        string fix;
        return w.Length > 0 && LiteralBoolFixes.TryGetValue(w, out fix)
            ? "invalid_literal: '" + w + "' is not a valid Boolean -- use '" + fix + "'"
            : null;
    }

    private static int CountSources(string sources)
    {
        int n = 0;
        if (!string.IsNullOrEmpty(sources))
            foreach (var s in sources.Split(',')) if (s.Trim().Length > 0) n++;
        return n;
    }

    // POST /bridge/expr/validate?expression=...&sources=comma,sep - syntax-check a
    // formula WITHOUT attaching it (the read-only sibling of the attach gate + the
    // ValidateExpression ExportMethod; all three share ValidateExpressionSyntax).
    private string ValidateExprJson(string firstLine)
    {
        string expr = QueryParam(firstLine, "expression");
        string sources = QueryParam(firstLine, "sources");
        if (string.IsNullOrEmpty(expr))
            return ErrorJson("bad_query", "required: expression (+ sources=comma,sep,node,paths)");
        int n = CountSources(sources);
        var err = ValidateExpressionSyntax(expr, n);
        if (err == null)
            return "{\"ok\":true,\"valid\":true,\"sources\":" + n + "}";
        return "{\"ok\":true,\"valid\":false,\"sources\":" + n + ",\"error\":\"" + JsonEscape(err) + "\"" +
               InvalidLiteralExtra(err) + "}";
    }

    // Diagnostic: reflect a CLR type's public methods + properties (walking the base
    // chain) by full name across loaded assemblies. Read-only introspection to discover
    // an Optix managed API without a doc - e.g. how to validate an ExpressionEvaluator
    // at design time. Same spirit as varmembers/typeinfo.
    private static string DiagClrTypeJson(string clrName)
    {
        if (string.IsNullOrEmpty(clrName))
            return "{\"error\":{\"code\":\"bad_query\",\"message\":\"required: name=Full.Clr.TypeName\"}}";
        System.Type t = null;
        foreach (var asm in System.AppDomain.CurrentDomain.GetAssemblies())
        {
            try { var c = asm.GetType(clrName); if (c != null) { t = c; break; } } catch { }
        }
        if (t == null)
            return "{\"error\":{\"code\":\"type_not_found\",\"message\":\"no loaded CLR type: " + JsonEscape(clrName) + "\"}}";
        var sb = new StringBuilder();
        sb.Append("{\"type\":\"" + JsonEscape(t.FullName) + "\",\"assembly\":\"" +
                  JsonEscape(t.Assembly.GetName().Name) + "\",\"chain\":[");
        bool firstLevel = true;
        for (var cur = t; cur != null && cur != typeof(object); cur = cur.BaseType)
        {
            if (!firstLevel) sb.Append(",");
            firstLevel = false;
            sb.Append("{\"level\":\"" + JsonEscape(cur.Name) + "\",\"methods\":[");
            bool f = true;
            foreach (var m in cur.GetMethods(BindingFlags.Public | BindingFlags.Instance |
                                             BindingFlags.Static | BindingFlags.DeclaredOnly))
            {
                if (m.IsSpecialName) continue;
                if (!f) sb.Append(",");
                f = false;
                var ps = string.Join(", ", m.GetParameters().Select(p => p.ParameterType.Name));
                sb.Append("\"" + JsonEscape(m.ReturnType.Name + " " + m.Name + "(" + ps + ")") + "\"");
            }
            sb.Append("],\"properties\":[");
            f = true;
            foreach (var p in cur.GetProperties(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly))
            {
                if (!f) sb.Append(",");
                f = false;
                sb.Append("\"" + JsonEscape(p.PropertyType.Name + " " + p.Name + (p.CanWrite ? " {s}" : " {r}")) + "\"");
            }
            sb.Append("]}");
            if (cur.Name == "NodeLogic" || cur.Name == "UAObject" || cur.Name == "UANode") break;
        }
        sb.Append("]}");
        return sb.ToString();
    }

    // Creates a child node whose BROWSE NAME must be qualified with the FTOptix.UI
    // namespace rather than the project's. Currently used only for
    // GridLayoutProperties: Studio writes it as `Name: ns=<FTOptix.UI>;
    // GridLayoutProperties` (confirmed 2026-09-05 in a Studio-authored
    // <project>/Nodes/UI/Templates/<Template>.yaml), and the layout engine honours
    // only that one - a project-qualified child takes RowStart/ColumnStart writes
    // that read back perfectly and render as nothing.
    //
    // What is namespaced is the QUALIFIED BROWSE NAME, not the NodeId: every
    // INSTANCE node lives in the project namespace (measured live: a materialised
    // GridLayoutProperties is NodeId ns=109 while FTOptix.UI is ns=8), so a check
    // on NodeId.NamespaceIndex can only ever fail. IUANode.QualifiedBrowseName is
    // the settable QualifiedName the qualifier rides on.
    //
    // Two-attempt order, each in its own try/catch (per SchemaDumpJson idiom):
    //   (a) parent's CLR proxy property getter - materialises the child from the
    //       type declaration the way GetOrCreateVariable does for a variable;
    //       re-qualify its browse name in place.
    //   (b) MakeObject against the resolved FTOptix.UI type, qualified BEFORE the
    //       Add so the parent indexes the child under its final name.
    //   (c) refuse with ui_namespace_child_unsupported - a silent fall-through to
    //       a project-namespace create is the original bug and stays unreachable.
    //
    // Post-creation: re-reads QualifiedBrowseName.NamespaceIndex and compares it
    // against the anchor's; on mismatch returns ok:false / wrong_namespace with
    // both indices and the created path so the caller can delete it.
    private string TryCreateUiNamespaceChild(IUANode parentNode, string childName, string parentPath)
    {
        int uiNsIdx = ResolveUiNamespaceIndex();
        string createdPath = parentPath + "/" + childName;

        // -- (a) Materialise via the parent's generated CLR proxy property --------
        // The generated Optix proxy (e.g. FTOptix.UI.GridLayout) exposes structural
        // children as CLR properties; calling GetValue triggers the SDK to
        // materialise the node from the type declaration - but under the PROJECT
        // browse-name qualifier, so it still has to be re-qualified.
        try
        {
            var pi = parentNode.GetType().GetProperty(childName,
                BindingFlags.Public | BindingFlags.Instance);
            if (pi != null)
            {
                var child = pi.GetValue(parentNode) as IUANode;
                if (child != null)
                {
                    string err = QualifyBrowseName(child, childName, uiNsIdx, createdPath);
                    if (err != null) return err;
                    return "{\"ok\":true,\"created_path\":\"" + JsonEscape(createdPath) + "\""
                           + ",\"namespace\":\"FTOptix.UI\",\"via\":\"proxy-property\""
                           + ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
                }
            }
        }
        catch { /* fall through to (b) */ }

        // -- (b) MakeObject against the FTOptix.UI type, qualified before the Add --
        try
        {
            if (uiNsIdx < 0)
                throw new Exception("FTOptix.UI namespace not resolvable: anchor lookup returned null");
            bool _via;
            var typeId = ResolveUiTypeId(childName, out _via);
            if (typeId == null)
                throw new Exception("'" + childName + "' ObjectType not found in FTOptix.UI");
            var child = InformationModel.MakeObject(childName, typeId);
            var qbn = child.QualifiedBrowseName;
            qbn.NamespaceIndex = uiNsIdx;
            child.QualifiedBrowseName = qbn;
            parentNode.Add(child);
            string err = QualifyBrowseName(child, childName, uiNsIdx, createdPath);
            if (err != null)
            {
                try { child.Delete(); } catch { }
                return err;
            }
            return "{\"ok\":true,\"created_path\":\"" + JsonEscape(createdPath) + "\""
                   + ",\"namespace\":\"FTOptix.UI\",\"via\":\"namespace-qualified-create\""
                   + ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch { /* fall through to (c) */ }

        // -- (c) Refuse -----------------------------------------------------------
        return ErrorJson("ui_namespace_child_unsupported",
            "'" + childName + "' must carry the FTOptix.UI browse-name qualifier; "
            + "proxy-property materialisation and namespace-qualified create both failed - "
            + "ensure the parent is a layout widget instance (not a project-namespace type) "
            + "and that bridge >= 1.0.8 is loaded");
    }

    // Force `node`'s browse name into namespace `uiNsIdx`, then READ BACK and prove
    // it. Returns null on success, a wrong_namespace error body otherwise - no
    // ok:true is ever emitted without this read-back.
    private string QualifyBrowseName(IUANode node, string childName, int uiNsIdx, string createdPath)
    {
        if (uiNsIdx < 0)
            return "{\"ok\":false,\"error\":\"wrong_namespace\""
                   + ",\"child_ns\":-1,\"ui_ns\":-1"
                   + ",\"created_path\":\"" + JsonEscape(createdPath) + "\"}";
        try
        {
            if (node.QualifiedBrowseName.NamespaceIndex != uiNsIdx)
            {
                var qbn = node.QualifiedBrowseName;
                qbn.NamespaceIndex = uiNsIdx;
                qbn.Name = childName;
                node.QualifiedBrowseName = qbn;
            }
        }
        catch { /* the read-back below is the verdict, not this assignment */ }
        int actual = -1;
        try { actual = node.QualifiedBrowseName.NamespaceIndex; } catch { }
        if (actual != uiNsIdx)
            return "{\"ok\":false,\"error\":\"wrong_namespace\""
                   + ",\"child_ns\":" + actual
                   + ",\"ui_ns\":" + uiNsIdx
                   + ",\"created_path\":\"" + JsonEscape(createdPath) + "\"}";
        return null;
    }

    // The FTOptix.UI namespace index, read from a browse-name-verified anchor;
    // -1 on any failure (never throws - consistent with SchemaDumpJson).
    private static int ResolveUiNamespaceIndex()
    {
        try
        {
            var anchor = UiTypeAnchor();
            return anchor == null ? -1 : anchor.NodeId.NamespaceIndex;
        }
        catch { return -1; }
    }

    // Unified browse-name resolver for UI type names. Callers pass the name
    // an author or agent typed; the method finds the right NodeId regardless of
    // whether the field constant and the live BrowseName are aligned.
    //
    // Step (a): field constant -> InformationModel.Get(nid); accepted ONLY when
    //   the resolved node is non-null AND node.BrowseName == name (aligned types
    //   take this fast path - the common case for ~102 builtin types).
    // Step (b): browse-name walk from a verified Panel anchor - walks the owner
    //   namespace scanning siblings for BrowseName==name with NodeClass in
    //   {ObjectType, VariableType}. Handles types whose field name differs from
    //   their BrowseName (e.g. VirtualKeyboard* catalog). Sets viaBrowseName=true.
    // Step (c): return null - callers keep their type_not_found / 404 behaviour.
    //
    // Each step is guarded by try/catch so a resolver that throws degrades to
    // null and never aborts catalog enumeration.
    private NodeId ResolveUiTypeId(string name, out bool viaBrowseName)
    {
        viaBrowseName = false;

        // Step (a): field constant lookup with BrowseName alignment check.
        try
        {
            var field = typeof(FTOptix.UI.ObjectTypes)
                .GetField(name, BindingFlags.Public | BindingFlags.Static);
            if (field != null && field.GetValue(null) is NodeId nid)
            {
                var node = InformationModel.Get(nid);
                if (node != null && node.BrowseName == name)
                    return nid;
            }
        }
        catch { /* resolver throwing degrades to null, never aborts */ }

        // Step (a2): the same field-constant lookup over EVERY loaded
        // FTOptix.<Module>.ObjectTypes catalog (Alarm, DataLogger, Core, Recipe,
        // Store, Report, ...). Alarm, logger and user objects and the
        // AlarmGrid/LoginForm widgets live in those catalogs, not in FTOptix.UI,
        // so create_object / create_type / create_widget could not name them
        // (measured 2026-09-13 on 1.0.8: DigitalAlarm, DataLogger, User,
        // AlarmGrid, LoginForm all type_not_found). "Module.Type" pins a module.
        try
        {
            var modId = ResolveModuleTypeId(name);
            if (modId != null) return modId;
        }
        catch { /* resolver throwing degrades to null, never aborts */ }

        // Step (b): browse-name walk of the builtin TYPE TREE from a verified anchor.
        // Owner of a type node is its SUPERTYPE (measured 2026-09-05 on the live
        // model: Label 8/10 -> Item 8/6 -> BaseUIObject 8/112 -> BaseObjectType
        // 0/58), so a scan of one anchor's siblings only ever sees that anchor's
        // supertype's direct subtypes - VirtualKeyboardTextButton derives from
        // Button, GridLayoutProperties straight from BaseObjectType, and neither
        // is a sibling of Panel. Climb to the topmost type node and DFS the whole
        // type tree from there.
        try
        {
            var anchor = UiTypeAnchor();
            if (anchor != null)
            {
                int uiNsIdx = anchor.NodeId.NamespaceIndex;
                var root = anchor;
                // Climb while the owner is still a type node; the last one is the
                // ObjectType root (BaseObjectType 0/58), whose owner is the
                // ObjectTypes folder (an Object, not a type).
                while (root.Owner != null && root.Owner.NodeClass == NodeClass.ObjectType)
                    root = root.Owner;
                // Widen two hops past that root to the Types folder (0/86): EVENT
                // types hang off BaseEventType 0/2041 under the SIBLING EventTypes
                // folder, not under BaseObjectType (measured 2026-09-05:
                // UserValueChanged 8/690 -> BaseEventType -> EventTypes), so a walk
                // rooted at BaseObjectType has an event-shaped blind spot. Never a
                // hard-coded id - both hops are relative to the verified anchor, and
                // a missing hop degrades to the narrower root.
                var searchRoot = root;
                if (searchRoot.Owner != null) searchRoot = searchRoot.Owner;
                if (searchRoot.Owner != null) searchRoot = searchRoot.Owner;
                var hit = FindTypeByBrowseName(searchRoot, name, uiNsIdx);
                if (hit != null)
                {
                    viaBrowseName = true;
                    return hit.NodeId;
                }
                // Step (b2): the same walk over every non-UI module namespace
                // (FTOptix.Alarm's DigitalAlarm and AlarmGrid, FTOptix.Core's
                // LoginForm/LoginButton, ...) for types whose catalog field name
                // is not their browse name, or whose catalog step (a2) missed.
                // The project namespace is excluded so a template named like a
                // builtin can never shadow it.
                hit = FindTypeByBrowseName(searchRoot, name, -1);
                if (hit != null)
                {
                    viaBrowseName = true;
                    return hit.NodeId;
                }
            }
        }
        catch { /* resolver throwing degrades to null, never aborts */ }

        // Step (c): not found.
        return null;
    }

    // A browse-name-VERIFIED node in the builtin UI type namespace: the field
    // constant for "Label" only counts when the node it resolves to is really
    // named Label. Everything that needs the FTOptix.UI namespace index (or a
    // foothold in the type tree) starts here so no index is ever hard-coded.
    private static IUANode UiTypeAnchor()
    {
        try
        {
            var f = typeof(FTOptix.UI.ObjectTypes)
                .GetField("Label", BindingFlags.Public | BindingFlags.Static);
            if (f != null && f.GetValue(null) is NodeId nid)
            {
                var node = InformationModel.Get(nid);
                if (node != null && node.BrowseName == "Label") return node;
            }
        }
        catch { /* anchor lookup never throws out of the resolver */ }
        return null;
    }

    // Depth-first search of the type tree under `root` for a type node whose
    // BrowseName is `name`, restricted to namespace `nsIdx` so a project type
    // can never answer for a builtin. Only type nodes and the type folders are
    // descended (the rest of a type's Children are its declared variables).
    // Bounded by a visit cap and a seen-set: the tree is a DAG by Owner, but the
    // cap keeps a pathological model from turning a catalog enumeration into a
    // hang. Reached only on a MISALIGNED name - an aligned type never leaves
    // ResolveUiTypeId step (a).
    private static IUANode FindTypeByBrowseName(IUANode root, string name, int nsIdx)
    {
        if (root == null || string.IsNullOrEmpty(name)) return null;
        var seen = new HashSet<string>();
        var stack = new Stack<IUANode>();
        stack.Push(root);
        int visited = 0;
        while (stack.Count > 0 && visited < 50000)
        {
            var cur = stack.Pop();
            if (cur == null) continue;
            string key;
            try { key = cur.NodeId.ToString(); } catch { continue; }
            if (!seen.Add(key)) continue;
            visited++;
            try
            {
                // nsIdx < 0 = any namespace except OPC UA base (0) and the
                // project's own (step (b2): module types outside FTOptix.UI).
                bool nsOk = nsIdx >= 0 ? cur.NodeId.NamespaceIndex == nsIdx
                          : (cur.NodeId.NamespaceIndex != 0 && cur.NodeId.NamespaceIndex != ProjectNsIdx());
                if (cur.BrowseName == name && nsOk &&
                    (cur.NodeClass == NodeClass.ObjectType || cur.NodeClass == NodeClass.VariableType))
                    return cur;
                foreach (var kid in cur.Children)
                {
                    // A type node carries its SUBTYPES as children; the Object-class
                    // nodes under Types are the ObjectTypes/EventTypes/... folders,
                    // so those are descended too. Everything else (a type's declared
                    // variables and methods) is a leaf for this search.
                    if (kid != null &&
                        (kid.NodeClass == NodeClass.ObjectType ||
                         kid.NodeClass == NodeClass.VariableType ||
                         kid.NodeClass == NodeClass.Object))
                        stack.Push(kid);
                }
            }
            catch { /* one unreadable node must not abort the walk */ }
        }
        return null;
    }

    // Step (a2) worker for ResolveUiTypeId: a public static NodeId field named
    // `name` on any loaded FTOptix.<Module>.ObjectTypes class (FTOptix.UI is
    // step (a)'s job and is skipped here), BrowseName-verified like step (a).
    // Accepts "FTOptix.Alarm.DigitalAlarm" to pin the module; a bare name takes
    // the first catalog that carries it.
    private static NodeId ResolveModuleTypeId(string name)
    {
        if (string.IsNullOrEmpty(name)) return null;
        string ns = null, field = name;
        int dot = name.LastIndexOf('.');
        if (dot > 0) { ns = name.Substring(0, dot); field = name.Substring(dot + 1); }
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            Type[] types;
            try { types = asm.GetTypes(); }
            catch (ReflectionTypeLoadException e) { types = e.Types; }
            catch { continue; }
            if (types == null) continue;
            foreach (var t in types)
            {
                if (t == null || t.Name != "ObjectTypes" || t.Namespace == null ||
                    !t.Namespace.StartsWith("FTOptix.") || t == typeof(FTOptix.UI.ObjectTypes))
                    continue;
                if (ns != null && t.Namespace != ns) continue;
                FieldInfo f;
                try { f = t.GetField(field, BindingFlags.Public | BindingFlags.Static); }
                catch { continue; }
                if (f == null || !(f.GetValue(null) is NodeId nid)) continue;
                // Unlike step (a), no BrowseName equality: the catalog's field
                // name IS the public C# name (DigitalAlarm's node may carry a
                // different browse name), and only the UI catalog has the
                // misaligned-constant history that made step (a) strict. A
                // loaded, type-class node behind the constant is enough.
                IUANode node = null;
                try { node = InformationModel.Get(nid); } catch { /* unloaded module */ }
                if (node != null && (node.NodeClass == NodeClass.ObjectType ||
                                     node.NodeClass == NodeClass.VariableType))
                    return nid;
            }
        }
        return null;
    }

    // The project's own namespace index (for step (b2)'s exclusion); -2 when
    // it cannot be read so the exclusion never accidentally matches ns 0.
    private static int ProjectNsIdx()
    {
        try { return Project.Current.NodeId.NamespaceIndex; } catch { return -2; }
    }

    private static NodeId ResolveEventType(string name)
    {
        var f = typeof(FTOptix.UI.ObjectTypes).GetField(name, BindingFlags.Public | BindingFlags.Static);
        if (f != null && f.GetValue(null) is NodeId id) return id;
        return null;
    }

    // The authoritative valid-event set for wire_event: the SAME reflection surface
    // ResolveEventType resolves against (public static NodeId fields of
    // FTOptix.UI.ObjectTypes whose name ends "Event"). By construction, everything
    // returned here WOULD resolve - so the reject-with-valid-list can never lie.
    private static System.Collections.Generic.List<string> ValidUiEventNames()
    {
        var names = new System.Collections.Generic.List<string>();
        foreach (var f in typeof(FTOptix.UI.ObjectTypes).GetFields(BindingFlags.Public | BindingFlags.Static))
        {
            if (f.FieldType == typeof(NodeId) && f.Name.EndsWith("Event"))
                names.Add(f.Name);
        }
        names.Sort(System.StringComparer.Ordinal);
        return names;
    }

    // Best-effort "did you mean" for a wrong event name. Normalizes both sides
    // (letters only, drop a trailing "event") and matches on containment either way,
    // so "click"/"Click"/"clickEvent" -> MouseClickEvent. Returns null on no match
    // (the valid_events list still carries the full authoritative set).
    private static string SuggestUiEvent(string given, System.Collections.Generic.List<string> valid)
    {
        var g = new string((given ?? "").ToLowerInvariant().Where(char.IsLetter).ToArray());
        if (g.EndsWith("event")) g = g.Substring(0, g.Length - 5);
        if (g.Length == 0) return null;
        foreach (var v in valid)
        {
            var n = v.ToLowerInvariant();
            if (n.EndsWith("event")) n = n.Substring(0, n.Length - 5);
            if (n == g || n.Contains(g) || g.Contains(n)) return v;
        }
        return null;
    }

    // Best-effort "did you mean" for a wrong property name, sharing SuggestUiEvent's
    // algorithm: normalize both sides (letters only, lowercased) and match on
    // containment either way, so "backgroundcolor"/"colour" -> ... (whatever the
    // type declares that contains or is contained by the input). Property names have
    // no "Event" suffix to trim, so normalization is just lowercase + strip-letters.
    // Returns null on no match (the valid_properties list still carries the full set).
    private static string SuggestPropertyName(string given, System.Collections.Generic.List<string> valid)
    {
        var g = new string((given ?? "").ToLowerInvariant().Where(char.IsLetter).ToArray());
        if (g.Length == 0) return null;
        foreach (var v in valid)
        {
            var n = v.ToLowerInvariant();
            if (n == g || n.Contains(g) || g.Contains(n)) return v;
        }
        return null;
    }

    // ---- model helpers ------------------------------------------------------

    // GET /bridge/map?path=UI&depth=6&max=800&ids=1
    // Project map: name/type outline of a subtree in ONE call - the cheap
    // alternative to walking with repeated /bridge/nodes. A depth-exhausted
    // node reports its hidden descendant count ("n") instead of children;
    // the global node budget ("max") stops expansion with per-parent "more"
    // counts - truncation is always explicit. Placeholder-collection children
    // carry their element type ("coll") so placement rules read off the tree.
    private int MapCountDescendants(IUANode node, int cap)
    {
        int total = 0;
        foreach (var c in node.Children)
        {
            total++;
            if (total >= cap) return total;
            total += MapCountDescendants(c, cap - total);
        }
        return total;
    }

    private void MapNodeJson(StringBuilder sb, IUANode node, string name,
                             int depth, ref int budget, bool ids, string collElem)
    {
        sb.Append("{\"name\":\"" + JsonEscape(name) + "\"");
        if (collElem != null)
            sb.Append(",\"coll\":\"" + JsonEscape(collElem) + "\"");
        else
            sb.Append(",\"type\":\"" + JsonEscape(node.GetType().Name) + "\"");
        if (ids)
            sb.Append(",\"id\":\"" + JsonEscape(node.NodeId.ToString()) + "\"");
        var deref = MapDeref(node);
        if (deref != null)
            sb.Append(",\"ref\":\"" + JsonEscape(deref) + "\"");
        var kids = node.Children.ToList();
        if (kids.Count > 0)
        {
            if (depth <= 0 || budget <= 0)
            {
                sb.Append(",\"n\":" + MapCountDescendants(node, 100000));
            }
            else
            {
                var collMap = new Dictionary<string, string>();
                foreach (var pi in node.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
                {
                    if (!IsPlaceholderColl(pi.PropertyType) && !IsPlaceholderRoColl(pi.PropertyType)) continue;
                    var elem = PlaceholderElementType(pi.PropertyType);
                    if (elem != null) collMap[pi.Name] = elem.Name;
                }
                sb.Append(",\"children\":[");
                int emitted = 0;
                foreach (var c in kids)
                {
                    if (budget <= 0) break;
                    budget--;
                    if (emitted++ > 0) sb.Append(",");
                    string ce;
                    collMap.TryGetValue(c.BrowseName, out ce);
                    MapNodeJson(sb, c, c.BrowseName, depth - 1, ref budget, ids, ce);
                }
                sb.Append("]");
                if (emitted < kids.Count)
                    sb.Append(",\"more\":" + (kids.Count - emitted));
            }
        }
        sb.Append("}");
    }

    private static bool MapIsFolder(IUANode node)
    { return node.GetType().Name.EndsWith("Folder"); }

    private static bool MapIsLeaf(IUANode node)
    {
        // variables / methods are plumbing at orientation altitude
        var nc = node.NodeClass.ToString();
        return nc.Contains("Variable") || nc.Contains("Method");
    }

    // Overview walk: folders expand recursively; a COMPONENT (non-folder
    // object) is a single line + descendant count - its properties appear
    // only when the caller scopes the map to it (mode auto -> full). Leaf
    // plumbing (variables/methods) folds into the parent's "skip" count.
    private void MapOverviewJson(StringBuilder sb, IUANode node, string name,
                                 int depth, ref int budget, bool ids, string collElem)
    {
        sb.Append("{\"name\":\"" + JsonEscape(name) + "\"");
        if (collElem != null)
            sb.Append(",\"coll\":\"" + JsonEscape(collElem) + "\"");
        else
            sb.Append(",\"type\":\"" + JsonEscape(node.GetType().Name) + "\"");
        if (ids)
            sb.Append(",\"id\":\"" + JsonEscape(node.NodeId.ToString()) + "\"");
        var kids = node.Children.ToList();
        if (kids.Count > 0)
        {
            if (!MapIsFolder(node) || depth <= 0 || budget <= 0)
            {
                // component boundary (or exhausted): compress to a count
                sb.Append(",\"n\":" + MapCountDescendants(node, 100000));
            }
            else
            {
                var collMap = new Dictionary<string, string>();
                foreach (var pi in node.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
                {
                    if (!IsPlaceholderColl(pi.PropertyType) && !IsPlaceholderRoColl(pi.PropertyType)) continue;
                    var elem = PlaceholderElementType(pi.PropertyType);
                    if (elem != null) collMap[pi.Name] = elem.Name;
                }
                sb.Append(",\"children\":[");
                int emitted = 0, skipped = 0;
                foreach (var c in kids)
                {
                    if (MapIsLeaf(c)) { skipped++; continue; }
                    if (budget <= 0) break;
                    budget--;
                    if (emitted++ > 0) sb.Append(",");
                    string ce;
                    collMap.TryGetValue(c.BrowseName, out ce);
                    MapOverviewJson(sb, c, c.BrowseName, depth - 1, ref budget, ids, ce);
                }
                sb.Append("]");
                int unshown = kids.Count - emitted - skipped;
                if (unshown > 0) sb.Append(",\"more\":" + unshown);
                if (skipped > 0) sb.Append(",\"vars\":" + skipped);
            }
        }
        sb.Append("}");
    }

    // Project-relative path of a node (walk the Owner chain up to the root).
    private string NodePathOf(IUANode node)
    {
        try
        {
            var parts = new List<string>();
            var cur = node;
            var rootId = Project.Current.NodeId;
            int guard = 0;
            while (cur != null && guard++ < 64)
            {
                if (cur.NodeId == rootId || cur.NodeId.Equals(rootId)) break;
                parts.Add(cur.BrowseName);
                cur = cur.Owner;
            }
            parts.Reverse();
            return string.Join("/", parts);
        }
        catch { return null; }
    }

    // ---- relative dynamic links inside a type -------------------------------
    //
    // FIELD FINDING 2026-09-04. `bind` writes the right thing without trying:
    // its property variable is ALREADY PARENTED when propVar.SetDynamicLink(src)
    // runs, so the SDK has a shared ancestor to be relative to and serializes the
    // relative form. The three other link writers do not have that luxury:
    //   attach_expression   builds its Source<n> variables DETACHED - the
    //                       ExpressionEvaluator only reaches the model at
    //                       SetConverter, after every source has been linked
    //   attach_formatter    the same, inside BuildFormattedLink
    //   wire_event(method)  ObjectPointer carries an absolute NodeId by design
    // A detached holder leaves the SDK nothing to be relative TO, so the link
    // serializes as an ABSOLUTE NodeId. Between unrelated subtrees that is
    // correct. Inside an ObjectType it is a silent bug: the absolute id names the
    // TYPE's own node, so every INSTANCE reads the type's variable (the value
    // looks static and never moves) or, for a method target, the runtime answers
    // "No behaviour or observer found that handles the method".
    //
    // The fix is to re-point the link at the relative path AFTER the structure is
    // attached, and ONLY when holder and target sit under the same ObjectType.

    // Nearest ObjectType at or above `node`, else null.
    private static IUANode NearestTypeAncestor(IUANode node)
    {
        int guard = 0;
        for (var cur = node; cur != null && guard++ < 64; cur = cur.Owner)
            if (cur is IUAObjectType) return cur;
        return null;
    }

    private static bool IsUnder(IUANode node, IUANode ancestor)
    {
        int guard = 0;
        for (var cur = node; cur != null && guard++ < 64; cur = cur.Owner)
            if (cur.NodeId.Equals(ancestor.NodeId)) return true;
        return false;
    }

    // Studio's own relative NodePath form, anchored at the VARIABLE THAT HOLDS the
    // link: one ".." per Owner step up to the common ancestor, then the browse
    // names down. The anchor is not a guess - it is what the two hand-built
    // relative paths in WireEventInline already count on ("../../../../" from
    // ObjectPointer up to the event node, "../../../../.." from a variable under
    // InputArguments to that same node), and this builder reproduces both literals
    // exactly. Null when the two nodes share no ancestor.
    private static string RelativeNodePath(IUANode linkHolder, IUANode target)
    {
        var ups = new List<IUANode>();
        for (var c = linkHolder; c != null && ups.Count < 64; c = c.Owner) ups.Add(c);
        var downs = new List<IUANode>();
        for (var c = target; c != null && downs.Count < 64; c = c.Owner) downs.Add(c);
        // ups[0] is the holder itself; a link path always starts with at least one
        // "..", so the ancestor search starts one level up.
        for (int u = 1; u < ups.Count; u++)
        {
            for (int d = 0; d < downs.Count; d++)
            {
                if (!ups[u].NodeId.Equals(downs[d].NodeId)) continue;
                var sb = new StringBuilder();
                for (int k = 0; k < u; k++) sb.Append(k + 1 < u ? "../" : "..");
                for (int k = d - 1; k >= 0; k--) sb.Append("/").Append(downs[k].BrowseName);
                return sb.ToString();
            }
        }
        return null;
    }

    // The path a link from `linkHolder` to `target` should carry, or null when the
    // two are not inside the same ObjectType - in which case the caller leaves the
    // absolute link exactly as the SDK wrote it.
    private static string SameTypeRelativePath(IUANode linkHolder, IUANode target)
    {
        try
        {
            var t = NearestTypeAncestor(linkHolder);
            if (t == null || !IsUnder(target, t)) return null;
            return RelativeNodePath(linkHolder, target);
        }
        catch { return null; }
    }

    // Re-point an ALREADY-WRITTEN dynamic link at its relative form. Returns the
    // path written, or null when nothing was rewritten (different types, or no
    // link materialized). Uses the same two node-model calls the raw bind does:
    // read the HasDynamicLink child, write its Value.
    private static string RelinkRelativeWithinType(IUAVariable linkHolder, IUANode target)
    {
        try
        {
            var rel = SameTypeRelativePath(linkHolder, target);
            if (rel == null) return null;
            var link = linkHolder.Refs.GetVariable(FTOptix.CoreBase.ReferenceTypes.HasDynamicLink);
            if (link == null) return null;
            link.Value = rel;
            return rel;
        }
        catch { return null; }
    }

    // Inline dereference for the detail walk: a NodePointer/Alias value is a
    // NodeId (resolved to a project path), a DynamicLink value is already a
    // NodePath string. Kills the describe-per-pointer round trip: a detail
    // map doubles as a wiring audit.
    private string MapDeref(IUANode node)
    {
        try
        {
            string tn = node.GetType().Name;
            var v = node as IUAVariable;
            if (v == null || v.Value == null) return null;
            if (tn == "NodePointer" || tn == "Alias")
            {
                var nid = v.Value.Value as NodeId;
                if (nid == null) return null;
                var target = InformationModel.Get(nid);
                if (target == null) return null;
                var p = NodePathOf(target);
                return string.IsNullOrEmpty(p) ? target.BrowseName : p;
            }
            if (tn == "DynamicLink")
            {
                // The stored NodePath is absolute ("/Objects/<Project>/Model/X")
                // or owner-relative ("../..") or an attribute ref ("...@BrowseName").
                // Re-render the absolute child form as the project-relative path
                // every bridge tool accepts (paste-ready); leave the other forms
                // as stored - they are not node targets.
                var raw = v.Value.Value as string;
                if (string.IsNullOrEmpty(raw)) return null;
                var absPrefix = "/Objects/" + Project.Current.BrowseName + "/";
                if (raw.StartsWith(absPrefix))
                    return raw.Substring(absPrefix.Length);
                return raw;
            }
        }
        catch { }
        return null;
    }

    // match= search: case-insensitive, '*' wildcard, against node NAME or
    // TYPE name. Returns flat full paths - ancestry rides in the path itself.
    private static bool MapMatch(string s, string pattern)
    {
        if (s == null) return false;
        var rx = "^" + System.Text.RegularExpressions.Regex.Escape(pattern)
            .Replace("\\*", ".*") + "$";
        return System.Text.RegularExpressions.Regex.IsMatch(
            s, rx, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    }

    private void MapSearch(IUANode node, string prefix, string pattern,
                           List<string> hits, ref int visited, int maxVisit, int maxHits)
    {
        foreach (var c in node.Children)
        {
            if (++visited > maxVisit || hits.Count >= maxHits) return;
            string p = prefix.Length == 0 ? c.BrowseName : prefix + "/" + c.BrowseName;
            string tn = c.GetType().Name;
            if (MapMatch(c.BrowseName, pattern) || MapMatch(tn, pattern))
                hits.Add("{\"path\":\"" + JsonEscape(p) + "\",\"type\":\"" + JsonEscape(tn) + "\"}");
            MapSearch(c, p, pattern, hits, ref visited, maxVisit, maxHits);
        }
    }

    private string MapSearchJson(string path, string pattern, int maxHits)
    {
        IUANode root = string.IsNullOrEmpty(path) ? Project.Current : ResolveNode(path);
        if (root == null) return null;
        var hits = new List<string>();
        int visited = 0;
        MapSearch(root, string.IsNullOrEmpty(path) ? "" : path.TrimEnd('/'),
                  pattern, hits, ref visited, 50000, maxHits);
        return "{\"path\":\"" + JsonEscape(path ?? "") + "\",\"mode\":\"search\"" +
               ",\"match\":\"" + JsonEscape(pattern) + "\"" +
               ",\"matches\":[" + string.Join(",", hits) + "]" +
               ",\"visited\":" + visited +
               ",\"hits_capped\":" + (hits.Count >= maxHits ? "true" : "false") + "}";
    }

    private string ProjectMapJson(string path, int depth, int max, bool ids, string mode)
    {
        IUANode root = string.IsNullOrEmpty(path) ? Project.Current : ResolveNode(path);
        if (root == null) return null;
        string rootName = string.IsNullOrEmpty(path)
            ? root.BrowseName : path.TrimEnd('/').Split('/').Last();
        // auto: orientation for folders, detail for a component. "detail" =
        // every node kind shown at the requested depth (NOT "fully expanded").
        string effective = mode == "full" ? "detail" : mode;
        if (mode == "auto")
            effective = MapIsFolder(root) ? "overview" : "detail";
        var sb = new StringBuilder();
        int budget = max;
        sb.Append("{\"path\":\"" + JsonEscape(path ?? "") + "\",\"mode\":\"" + effective + "\",\"map\":");
        if (effective == "overview")
            MapOverviewJson(sb, root, rootName, Math.Max(depth, 8), ref budget, ids, null);
        else
            MapNodeJson(sb, root, rootName, depth, ref budget, ids, null);
        // (effective mode rides in the header so callers never misread
        //  "detail at depth 1" as "the whole tree")
        sb.Append(",\"budget_left\":" + budget + "}");
        return sb.ToString();
    }

    private IUANode ResolveNode(string path)
    {
        try { return Project.Current.Get(path); }
        catch { return null; }
    }

    // ResolveNode, then a fallback for nodes whose BROWSE NAME contains the
    // path separator: a StoreColumn behind a recipe array is literally named
    // "/RecipeSettings_0", which Get() cannot spell. Walk separators from
    // the right; the first prefix that resolves is the parent, and the rest of
    // the string (slash and all) is matched against its children's BrowseName.
    // "DataStores/DB/Tables/T/Columns//RecipeSettings_9" -> parent ".../Columns",
    // child "/RecipeSettings_9".
    private IUANode ResolveNodeLoose(string path)
    {
        var n = ResolveNode(path);
        if (n != null || string.IsNullOrEmpty(path)) return n;
        for (int i = path.Length - 1; i > 0; i--)
        {
            if (path[i] != '/') continue;
            var parent = ResolveNode(path.Substring(0, i));
            if (parent == null) continue;
            string rest = path.Substring(i + 1);
            foreach (var child in parent.Children)
                if (child.BrowseName == rest) return child;
        }
        return null;
    }

    private string DataTypeName(IUAVariable v)
    {
        try
        {
            var dt = InformationModel.Get(v.DataType);
            return dt != null ? dt.BrowseName : v.DataType.ToString();
        }
        catch { return "unknown"; }
    }

    // Datatype for DESCRIBE output: array-typed variables get a "[]" suffix
    // ("NodeId[]") so callers can see the array-ness that set_property rejects
    // (unsupported_array_write) - DataTypeName alone hides it.
    private string DataTypeNameFull(IUAVariable v)
    {
        return DataTypeName(v) + (IsArrayVariable(v) ? "[]" : "");
    }

    // root-cause fix for a false "broken link" diagnosis.
    // describe_node (NodeJson) called this for EVERY property, including
    // DynamicLink/Alias children, via UAValue.ToString() - which returns
    // blank for a NodePath-boxed value (confirmed live: Studio's own
    // Properties panel showed VFD_2411's INPUT_PATH/OUTPUT_PATH/SETTINGS_PATH
    // fully populated with template-placeholder paths like
    // ".../VFD_{#id}&:I@NodeId", while describe_node reported them as empty
    // strings, 3 aliases x 9 VFD instances - a tooling bug, not a project
    // defect). MapDeref (used by the project-map tree) already had the correct
    // extraction for this exact case: unwrap UAValue.Value as a string FIRST
    // (works for NodePath/String-backed values), and only fall back to
    // ToString() for value types (Int32, Boolean, enums, ...) where ToString()
    // is correct. Copying that proven pattern here instead of leaving
    // describe_node on the broken path.
    private string ValueString(IUAVariable v)
    {
        try
        {
            if (v.Value == null) return "null";
            var raw = v.Value.Value as string;
            if (raw != null) return raw;
            return v.Value.ToString();
        }
        catch { return "unreadable"; }
    }

    // ---- HTTP / JSON plumbing ----------------------------------------------

    private static void WriteResponse(NetworkStream stream, string status, string body)
    {
        byte[] bodyBytes = Encoding.UTF8.GetBytes(body);
        string headers =
            "HTTP/1.1 " + status + "\r\n" +
            "Content-Type: application/json\r\n" +
            "Content-Length: " + bodyBytes.Length + "\r\n" +
            "Connection: close\r\n\r\n";
        byte[] headerBytes = Encoding.ASCII.GetBytes(headers);
        stream.Write(headerBytes, 0, headerBytes.Length);
        stream.Write(bodyBytes, 0, bodyBytes.Length);
        stream.Flush();
    }

    private static string QueryParam(string firstLine, string key)
    {
        int q = firstLine.IndexOf('?');
        if (q < 0) return null;
        int sp = firstLine.IndexOf(' ', q);
        string query = sp > q ? firstLine.Substring(q + 1, sp - q - 1) : firstLine.Substring(q + 1);
        foreach (var pair in query.Split('&'))
        {
            var kv = pair.Split(new[] { '=' }, 2);
            if (kv.Length == 2 && kv[0] == key)
            {
                try { return Uri.UnescapeDataString(kv[1]); }
                catch { return kv[1]; }
            }
        }
        return null;
    }

    private static string ErrorJson(string code, string message)
    {
        return "{\"error\":{\"code\":\"" + JsonEscape(code) +
               "\",\"message\":\"" + JsonEscape(message) + "\"" +
               InvalidLiteralExtra(message) + "}}";
    }

    // 1.0.8: ValidateExpressionSyntax's two literal-lint checks
    // return a plain string prefixed "invalid_literal: ", ending "...use <fix>"
    // or "...use '<fix>'". Every JSON body that carries that string as its error
    // message gets a structured {reason, fix} sibling here, so a caller (or an
    // LLM) can read the fix value without parsing prose. Returns "" (no extra
    // field) for every other error class -- this must never fire on a message
    // that merely CONTAINS "invalid_literal" mid-sentence, only one that IS the
    // literal-lint's own message shape.
    private static string InvalidLiteralExtra(string message)
    {
        const string prefix = "invalid_literal: ";
        if (message == null || !message.StartsWith(prefix)) return "";
        string reason = message.Substring(prefix.Length);
        int idx = reason.LastIndexOf("use ", StringComparison.Ordinal);
        string fix = idx >= 0 ? reason.Substring(idx + 4).Trim().Trim('\'') : "";
        return ",\"invalid_literal\":{\"reason\":\"" + JsonEscape(reason) +
               "\",\"fix\":\"" + JsonEscape(fix) + "\"}";
    }

    private static string Bool(bool b) { return b ? "true" : "false"; }

    // Type + message for an exception, the payload of every write handler's catch.
    private static string ExcMsg(Exception ex) { return ex.GetType().Name + ": " + ex.Message; }

    private static string JsonEscape(string s)
    {
        if (s == null) return "";
        return s.Replace("\\", "\\\\").Replace("\"", "\\\"")
                .Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t");
    }
}

