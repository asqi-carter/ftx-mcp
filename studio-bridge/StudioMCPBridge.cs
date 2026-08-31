using System;
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
    // wrong about behavior). Last bumped for the StopBridge port-sweep + bind
    // retry fix, which IS a real behavior change to this file.
    private const string BridgeVersion = "1.0.7";
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
                try { client = lst.AcceptTcpClient(); HandleClient(client); }
                catch (SocketException) { break; }
                catch (Exception ex) { Log.Warning("StudioBridge", "request error: " + ex.Message); }
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
            catch (Exception ex)
            {
                body = ErrorJson("internal", ex.Message);
                status = "500 Internal Server Error";
            }

            // U21: one Output line per MUTATION (never for reads — they burst).
            // Sited here, after the dispatch chain and the catch, because this
            // is the single point where every route's FINAL body passes on its
            // way to the one WriteResponse — including the internal-error path.
            MaybeLogMutation(firstLine, body);

            WriteResponse(stream, status, body);
        }
    }

    // ---- U21: per-mutation Output logging -----------------------------------

    // Routes that MUTATE the model — the only ones that log. Keep in sync with
    // the POST branches in the dispatcher (model/*, ui/widget, i18n/translation,
    // setup/web-engine, and node/{property,bind,alias,move,convert-to-type,
    // reorder,delete,event,attach-expression}). Read routes are deliberately
    // absent, and so is POST /bridge/expr/validate — a POST by shape but a pure
    // read (it validates, mutates nothing).
    private static readonly string[] _MutationRoutes = {
        "POST /bridge/model/", "POST /bridge/ui/widget",
        "POST /bridge/i18n/translation", "POST /bridge/setup/web-engine",
        "POST /bridge/node/property", "POST /bridge/node/bind",
        "POST /bridge/node/alias", "POST /bridge/node/move",
        "POST /bridge/node/convert-to-type", "POST /bridge/node/reorder",
        "POST /bridge/node/delete", "POST /bridge/node/event",
        "POST /bridge/node/attach-expression",
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
        // "<VERB> <route>" + path=/name= query params. NEVER the `value` param —
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
        // "port" lets the Python service, which now probes a whole
        // port RANGE (see StartListener), confirm which port answered rather
        // than assuming the well-known 8768 - and is handy for a human reading
        // the raw JSON while debugging which Studio instance is which.
        return "{\"bridge_version\":\"" + BridgeVersion +
               "\",\"project\":\"" + JsonEscape(project) +
               "\",\"model_loaded\":" + Bool(modelLoaded) +
               ",\"port\":" + _boundPort + "}";
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
                                "\",\"dotnet_type\":\"" + JsonEscape(child.GetType().Name) + "\"}");
            }
        }

        return "{\"path\":\"" + JsonEscape(path) +
               "\",\"browse_name\":\"" + JsonEscape(node.BrowseName) +
               "\",\"node_class\":\"" + node.NodeClass +
               "\",\"dotnet_type\":\"" + JsonEscape(node.GetType().Name) +
               "\",\"children\":[" + children + "]" +
               ",\"properties\":[" + props + "]" +
               ",\"truncated\":" + Bool(childTrunc || propTrunc) + "}";
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
            try
            {
                if (f.GetValue(null) is NodeId nid)
                {
                    var t = InformationModel.Get(nid);
                    if (t != null) browse = t.BrowseName;
                }
            }
            catch { /* unresolved type id - fall back to the field name */ }
            if (count++ > 0) sb.Append(",");
            sb.Append("{\"name\":\"" + JsonEscape(f.Name) +
                      "\",\"browse_name\":\"" + JsonEscape(browse) + "\"}");
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
                if (f.GetValue(null) is NodeId nid)
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
                             JsonEscape(browse) + "\",\"properties\":[" + props + "]}");
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
        var field = typeof(FTOptix.UI.ObjectTypes)
            .GetField(typeName, BindingFlags.Public | BindingFlags.Static);
        if (field == null) return null;
        var nid = field.GetValue(null) as NodeId;
        var t = nid != null ? InformationModel.Get(nid) : null;
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
               "\",\"browse_name\":\"" + JsonEscape(t.BrowseName) +
               "\",\"properties\":[" + props + "]" +
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
                    if (i > 0) added.Append(",");
                    added.Append("\"" + JsonEscape(s) + "\"");
                    i++;
                }
            }
            propVar.SetConverter(ee);
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path) + "\",\"name\":\"" + JsonEscape(name) +
                   "\",\"expression\":\"" + JsonEscape(expr) + "\",\"sources\":[" + added +
                   "],\"via\":\"expression-converter\",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
    }

    // POST /bridge/node/reorder?path=X&position=front|back  (or &index=N) - change a
    // node's z-order among its siblings. In Optix render order = child order (a
    // HasOrderedComponent list): last child renders on TOP (front), first renders at
    // the BACK. This is Studio's "bring to front / send to back" (drag up/down), and
    // the enabler for a Panel background Rectangle behind existing children (the
    // panelbg gap). Rebuilds the parent's Children in the new order using only
    // node-model Add/Remove (off-thread-safe class). SCRATCH-TEST before trusting.
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
            var kids = parent.Children.ToList();
            int cur = kids.IndexOf(node);
            if (cur < 0) return ErrorJson("not_a_child", "node is not in its parent's Children: " + path);
            int target;
            if (pos == "front") target = kids.Count - 1;   // last in list = rendered in FRONT
            else if (pos == "back") target = 0;            // first in list = rendered BEHIND
            else if (!int.TryParse(idxStr, out target))
                return ErrorJson("bad_query", "need position=front|back or index=<int>");
            if (target < 0) target = 0;
            if (target > kids.Count - 1) target = kids.Count - 1;
            if (target == cur)
                return "{\"ok\":true,\"path\":\"" + JsonEscape(path) + "\",\"index\":" + cur + ",\"noop\":true}";
            // Non-destructive in-place reorder via MoveUp()/MoveDown() (Sort-project-nodes:
            // the sanctioned API). MoveUp -> earlier in the child list = toward the BACK;
            // MoveDown -> later = toward the FRONT. NOTE: only effective on graphic objects
            // that live inside a TYPE (ScreenType/PanelType) - a plain instance's children
            // won't move. Reload the runtime page to see the visual effect.
            int moved = 0;
            while (cur > target) { node.MoveUp(); cur--; moved++; }
            while (cur < target) { node.MoveDown(); cur++; moved++; }
            return "{\"ok\":true,\"path\":\"" + JsonEscape(path) + "\",\"to\":" + target +
                   ",\"moves\":" + moved + ",\"mode\":\"inline\",\"thread\":\"http-bg\"}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + JsonEscape(ExcMsg(ex)) + "\"}";
        }
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
            return null;                                   // FTOptix-declared -> safe
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
            case "DisplayName": case "BrowseName": case "Description":
            case "NodeId": case "NodeClass":
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
            NodeId typeId = OpcUa.ObjectTypes.BaseObjectType;
            string typeLabel = "BaseObjectType";
            if (!string.IsNullOrEmpty(typePath))
            {
                var typeNode = ResolveNode(typePath);
                if (typeNode == null)
                    return ErrorJson("type_not_found", "no node at type path: " + typePath);
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
                NodeId baseId = null;
                var typeField = typeof(FTOptix.UI.ObjectTypes)
                    .GetField(baseName, BindingFlags.Public | BindingFlags.Static);
                if (typeField != null && typeField.GetValue(null) is NodeId nid)
                    baseId = nid;
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
            var typeField = typeof(FTOptix.UI.ObjectTypes)
                .GetField(typeName, BindingFlags.Public | BindingFlags.Static);
            if (typeField == null || !(typeField.GetValue(null) is NodeId typeId))
                return ErrorJson("type_not_found", "no builtin UI type: " + typeName);
            // The ObjectTypes catalog also carries the ABSTRACT layout bases in the
            // Item -> Container -> Panel chain. `Item`/`Container` are not concrete
            // renderable widgets: a bare instance is "not a UI object type" to the
            // WebPresentationEngine and CRASHES the render tree, killing every
            // sibling after it (found live 2026-07-25 — an agent picked "Container"
            // as a layout widget and lost the screen). Refuse loud and redirect.
            // (A full reflect-the-runtime-proxy renderability filter is a follow-up;
            // these two are the only bases an author realistically mistakes for a
            // widget.)
            if (typeName == "Item" || typeName == "Container")
                return ErrorJson("not_renderable",
                    "'" + typeName + "' is an abstract layout base, not a renderable "
                    + "widget — a bare instance crashes the render tree. Use 'Panel' "
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
        string raw  = QueryParam(firstLine, "value") ?? "";
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
            IUAVariable mvar = null;
            if (node is IUAObject asObject)
                mvar = asObject.GetOrCreateVariable(name);
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
            case "Int16": case "Int32": case "Int64":
            case "UInt16": case "UInt32": case "UInt64": case "Byte": case "SByte":
                {
                    // TryParse (not Convert.ToInt32) so a non-numeric value returns a
                    // clean bad_value instead of leaking a raw FormatException.
                    long iv;
                    if (!long.TryParse(raw, System.Globalization.NumberStyles.Integer,
                                       System.Globalization.CultureInfo.InvariantCulture, out iv))
                        return "value must be an integer for " + dt + ": " + raw;
                    v.Value = (int)iv;   // (int) truncates rather than throwing on range
                }
                break;
            case "Float": case "Double": case "Size":
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
    //                        accumulates this batch's creates/deletes, so
    //                        "create X then set X.Prop" validates clean and the
    //                        reverse order does not
    //   3 lint             - warnings only; `strict` promotes them to errors
    //
    // Body: {"ops":[{"op":"...", ...}], "strict":false}
    // Reply: {"ok":bool, "op_count":N, "errors":[{op_index,code,message,...}],
    //         "warnings":[{op_index,code,message}]}
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
                var created = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                var deleted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

                // PRE-PASS: every path this batch creates, regardless of position.
                // The sequential `created` set above is what decides validity (a
                // forward reference IS an error). This set exists only to make the
                // error ACTIONABLE: the reversed-order mistake is by definition a
                // forward reference, so at the point it is caught the sequential
                // set is still empty and could not name the culprit.
                var willCreate = new List<string>();
                foreach (var pre in opsEl.EnumerateArray())
                {
                    string pv = JsonStr(pre, "op");
                    if (pv == null || !pv.StartsWith("create", StringComparison.Ordinal)) continue;
                    string pn = JsonStr(pre, "name");
                    if (string.IsNullOrEmpty(pn)) continue;
                    string pp = ParentKey(pre);
                    willCreate.Add(string.IsNullOrEmpty(pp) ? pn : pp.TrimEnd('/') + "/" + pn);
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
                            ValidateOnNode(idx, verb, path, name, value, created, deleted,
                                           willCreate, addErr, addWarn);
                            break;

                        case "delete":
                            {
                                if (string.IsNullOrEmpty(path))
                                { addErr(idx, "bad_op", verb + " requires \"path\"", null); break; }
                                if (deleted.Contains(path))
                                { addErr(idx, "already_deleted", "op deletes '" + path + "' twice in this batch", null); break; }
                                if (ResolveNode(path) == null && !created.ContainsKey(path))
                                    addErr(idx, "unresolved_reference",
                                           "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                                deleted.Add(path);
                                created.Remove(path);
                            }
                            break;

                        case "move":
                        case "reorder":
                        case "wire_event":
                            {
                                if (string.IsNullOrEmpty(path))
                                { addErr(idx, "bad_op", verb + " requires \"path\"", null); break; }
                                if (deleted.Contains(path))
                                { addErr(idx, "modifies_deleted_node", verb + " targets '" + path + "', deleted earlier in this batch", null); break; }
                                if (ResolveNode(path) == null && !created.ContainsKey(path))
                                    addErr(idx, "unresolved_reference",
                                           "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                            }
                            break;

                        case "create_node":
                        case "create_widget":
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
                                    else if (ResolveNode(parentPath) == null && !created.ContainsKey(parentPath))
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
                                created[newPath] = typeName ?? "";
                                deleted.Remove(newPath);
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
        Dictionary<string, string> created, HashSet<string> deleted,
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

        var node = ResolveNode(path);
        if (node == null)
        {
            string hypoType;
            if (!created.TryGetValue(path, out hypoType))
            {
                addErr(idx, "unresolved_reference",
                       "no node at '" + path + "'" + NearestHint(path, willCreate), null);
                return;
            }
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
            case "Int16": case "Int32": case "Int64":
            case "UInt16": case "UInt32": case "UInt64": case "Byte": case "SByte":
                {
                    long iv;
                    if (!long.TryParse(raw, System.Globalization.NumberStyles.Integer,
                                       System.Globalization.CultureInfo.InvariantCulture, out iv))
                        return "value must be an integer for " + dt + ": " + raw;
                    return null;
                }
            case "Float": case "Double": case "Size":
                {
                    double dv;
                    if (!double.TryParse(raw, System.Globalization.NumberStyles.Float,
                                         System.Globalization.CultureInfo.InvariantCulture, out dv))
                        return "value must be a number for " + dt + ": " + raw;
                    return null;
                }
            case "NodeId":
                return ResolveNode(raw) == null
                    ? "NodeId value must be a resolvable node path: " + raw : null;
            case "Color":
                return CheckColor(raw);
            default:
                return CheckEnumOrRaw(dt, raw);
        }
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
        // TextHorizontalAlignment, alignment, ...) correctly — no hardcoded
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
            case "VerticalAlignment":   return new[] { "Top", "Bottom", "Center", "Stretch" };
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
    // order, so "Bottom" set ordinal 2 (=Center) and rendered centered — a live
    // build burned ~14 tool calls reverse-engineering this. Do NOT "fix" Center
    // back to 1; that reintroduces the swap. (Note the sibling Content*/Text*
    // alignment enums DO use the standard Center=1 order — different enums.)
    private static bool TryEnumOrdinal(string dt, string name, out int ord)
    {
        ord = 0;
        string key = (dt ?? "") + "." + (name ?? "").Trim().ToLowerInvariant();
        switch (key)
        {
            case "HorizontalAlignment.left":    ord = 0; return true;
            case "HorizontalAlignment.right":   ord = 1; return true;
            case "HorizontalAlignment.center":  ord = 2; return true;
            case "HorizontalAlignment.stretch": ord = 3; return true;
            case "VerticalAlignment.top":       ord = 0; return true;
            case "VerticalAlignment.bottom":    ord = 1; return true;
            case "VerticalAlignment.center":    ord = 2; return true;
            case "VerticalAlignment.stretch":   ord = 3; return true;
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
            if (srcVar != null)
            {
                propVar.SetDynamicLink(srcVar, mode);
                return "{\"ok\":true,\"path\":\"" + JsonEscape(path + "/" + name) +
                       "\",\"source\":\"" + JsonEscape(source) +
                       "\",\"mode\":\"" + JsonEscape(modeStr) + "\",\"via\":\"dynamiclink\"}";
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
                   ",\"note\":\"literal NodePath - resolves per instance at runtime; render-verify\"}";
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
    // CONFIRMED HAZARD, not theoretical. Calling
    // SearchBrokenDynamicLinks' FindBrokenDynamicLink through this endpoint killed
    // the whole FTOptixStudio.exe process outright - reproduced twice, across two
    // separate Studio sessions (crash, user relaunched Studio + StartBridge, called
    // it again, crashed again). No exception was ever caught by the try/catch below;
    // the process just died, which on .NET means something fatal happened on a
    // thread this handler doesn't control (most likely: this TcpListener accept
    // thread is NOT Studio's main/UI thread, and this particular built-in tool -
    // presumably written assuming it's driven by the UI's own Execute gesture -
    // touches something UI-thread-affine and crashes hard off-thread). This file's
    // OWN _bridge_write docstring already documents this exact class of problem for
    // property writes ("Several target endpoints require the bridge's
    // main-thread-marshaled write path... until that ships the bridge replies
    // not_implemented") - ExecuteMethod needs the same treatment, which it does NOT
    // have yet. Until proper main-thread marshaling is added here, treat EVERY call
    // through this endpoint as able to crash Studio, not just this one method - a
    // custom, UI-free [ExportMethod] may well be fine, but that has not been
    // verified either. For finding/fixing broken links specifically, use Studio's
    // own right-click Execute instead (the safe, supported path) until this is
    // fixed properly.
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
               "\",\"output_args\":[" + outSb + "]}";
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
    //   - CUSTOM METHOD: method=ObjectPath/MethodName (an object owning an [ExportMethod]).
    // ObjectPointer is a typed FTOptix.Core.NodePointer (the dispatcher resolves the call
    // target through it; a plain NodeId wires but never invokes). FTOptix.Core/.CoreBase
    // are referenced, so the fully-qualified names add no module ref. EventHandler is
    // fully-qualified to dodge the System.EventHandler ambiguity (CS0104). Node-attach
    // order matches the sample: container -> ObjectPointer, Method, InputArguments; then
    // populate InputArguments after it's parented.
    private string WireEventInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string evt = QueryParam(firstLine, "event");
        string command = QueryParam(firstLine, "command");
        string method = QueryParam(firstLine, "method");
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
            IUAVariable cmdTargetVar = null;   // command mode only
            string cmdValueRaw = null;         // command mode, Set only
            bool cmdNeedsValue = false;
            if (!string.IsNullOrEmpty(command))
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
                    case "SetVariable": case "Set":
                        methodName = "Set"; cmdNeedsValue = true;
                        cmdValueRaw = QueryParam(firstLine, "value") ?? "";
                        break;
                    case "ToggleVariable": case "Toggle":
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
                methodName = method.Substring(slash + 1);
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

            string via = string.IsNullOrEmpty(command) ? "eventhandler" : "command:" + methodName;
            return "{\"ok\":true,\"node\":\"" + JsonEscape(path) + "\",\"event\":\"" + JsonEscape(evt) +
                   "\",\"via\":\"" + JsonEscape(via) + "\"}";
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

    // Coerce a query-string value to a target variable's DataType (mirror of the
    // SetPropertyInline switch; kept separate so that validated path is untouched).
    // Returns UAValue (not object) so each typed return implicitly converts - an
    // `object` would need an explicit (UAValue) cast at the assignment site (CS0266).
    private UAValue CoerceRaw(string dtName, string raw, string firstLine)
    {
        switch (dtName)
        {
            case "Boolean": return (raw == "true" || raw == "1" || raw == "True");
            case "Int16": case "Int32": case "Int64":
            case "UInt16": case "UInt32": case "UInt64": case "Byte": case "SByte":
                return Convert.ToInt32(raw);
            case "Float": case "Double": case "Size":
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

    // Structural validation of an ExpressionEvaluator formula. Optix exposes NO
    // design-time parser (confirmed by reflection: ExpressionEvaluator has only
    // Expression/ExpressionVariable + inherited Start/Stop) and validates a formula
    // only at RUNTIME (a bad one silently no-ops). This catches the common author-time
    // mistakes WITHOUT reimplementing the grammar: unbalanced ()/{}, out-of-range {N}
    // placeholders, unknown function names, unterminated strings, number+string concat.
    // String literals are skipped so parens/braces inside them don't false-positive.
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
            if (c == '"') { inStr = true; word.Clear(); continue; }
            if (c == '(')
            {
                string w = word.ToString();
                if (w.Length > 0 && char.IsLetter(w[0]) && !ExprFunctions.Contains(w))
                    return "unknown function '" + w + "' (valid: " + string.Join(", ", ExprFunctions) + ")";
                paren++; word.Clear(); continue;
            }
            if (c == ')')
            {
                paren--;
                if (paren < 0) return "unbalanced parentheses: ')' with no matching '('";
                word.Clear(); continue;
            }
            if (c == '}') return "unbalanced braces: '}' with no matching '{'";
            if (c == '{')
            {
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
            if (char.IsLetterOrDigit(c) || c == '_') word.Append(c);
            else word.Clear();
        }
        if (inStr) return "unterminated string literal";
        if (paren > 0) return "unbalanced parentheses: " + paren + " unclosed '('";
        return null;
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
        return "{\"ok\":true,\"valid\":false,\"sources\":" + n + ",\"error\":\"" + JsonEscape(err) + "\"}";
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
               "\",\"message\":\"" + JsonEscape(message) + "\"}}";
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

