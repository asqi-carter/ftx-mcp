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
    private const string BridgeVersion = "1.0.1";
    // Cross-ALC stop signal: at design time Studio runs each [ExportMethod] in an
    // ISOLATED AssemblyLoadContext, so StartBridge and StopBridge share NO managed
    // state (neither instance NOR static - both were tried and failed).
    // A named kernel event IS shared across ALCs in the process; StopBridge sets it and
    // the accept loop (in whichever ALC owns the listener) polls it and closes the socket.
    private const string StopEventName = "Local\\StudioMCPBridge_Stop_p8768";
    private const int Port = 8768;   // loopback bridge port (client cfg.bridge_url)
    private const int MaxItems = 500;
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

    // Right-click this node -> Execute ValidateExpression to syntax-check an
    // ExpressionEvaluator formula BEFORE wiring it (Optix only validates at runtime,
    // where a bad formula silently no-ops). Result goes to the Studio Output. The
    // bridge's attach-expression endpoint + POST /bridge/expr/validate run the SAME
    // ValidateExpressionSyntax check, so the operator and the model client agree.
    [ExportMethod]
    public void ValidateExpression(string expression, string sources)
    {
        int n = CountSources(sources);
        var err = ValidateExpressionSyntax(expression, n);
        if (err == null)
            Log.Info("StudioBridge", "ValidateExpression OK (" + n + " source(s)): " + expression);
        else
            Log.Error("StudioBridge", "ValidateExpression INVALID: " + err + "  [" + expression + "]");
    }

    // Visible setup action: right-click this NetLogic node in Studio -> SetupProject.
    // Ensures a Web presentation engine exists (Port/Protocol/StartWindow/StyleSheet/
    // MaxNumberOfConnections) so the deployed runtime can serve a browser canvas. Runs
    // directly against Project.Current at design time - does NOT need the bridge started.
    [ExportMethod]
    public void SetupProject()
    {
        Log.Info("StudioBridge", "SetupProject: " + EnsureWebEngineCore(WebEnginePort, "0.0.0.0"));
    }

    // Create-or-open the process-global stop event (ManualReset). Shared across ALCs
    // by name, so a StopBridge in one ALC can signal a listener loop in another.
    private static EventWaitHandle OpenStopEvent()
    {
        return new EventWaitHandle(false, EventResetMode.ManualReset, StopEventName);
    }

    private void StartListener(string via)
    {
        // Clear any prior stop-signal so the fresh loop doesn't exit immediately.
        try { using (var ev = OpenStopEvent()) ev.Reset(); } catch { /* ignore */ }
        try
        {
            var listener = new TcpListener(IPAddress.Loopback, Port);
            listener.Start();               // exclusive bind (no SO_REUSEADDR - a clear
                                            // "in use" beats a silent double-bind)
            _listener = listener;
            _running = true;
            new Thread(Loop) { IsBackground = true, Name = "StudioBridge" }.Start();
            Log.Info("StudioBridge", "listening on http://127.0.0.1:" + Port +
                     " (started via " + via + ")");
        }
        catch (SocketException ex)
        {
            _running = false; _listener = null;
            Log.Error("StudioBridge", "cannot bind 127.0.0.1:" + Port + " (" + ex.Message +
                "). Another bridge still holds it - StopBridge (any project) frees it, or " +
                "close+reopen that Studio to drop an orphaned-reload listener.");
        }
        catch (Exception ex)
        {
            _running = false; _listener = null;
            Log.Error("StudioBridge", "failed to start on port " + Port + ": " + ex.Message);
        }
    }

    private void StopListener() { StopListener(false); }

    private void StopListener(bool quiet)
    {
        // The cross-ALC signal (the only thing that reliably reaches the running loop).
        try { using (var ev = OpenStopEvent()) ev.Set(); }
        catch (Exception ex) { Log.Warning("StudioBridge", "stop-signal failed: " + ex.Message); }
        // Best-effort same-ALC teardown too (harmless when state isn't shared).
        _running = false;
        var l = _listener; _listener = null;
        try { l?.Stop(); } catch { /* ignore */ }
        if (!quiet) Log.Info("StudioBridge", "stop signalled (port " + Port + " releasing)");
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
    private void Loop()
    {
        EventWaitHandle stopEv = null;
        try { stopEv = OpenStopEvent(); } catch { /* poll _running only */ }
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
            try { stopEv?.Dispose(); } catch { /* ignore */ }
            Log.Info("StudioBridge", "listener loop exited; port " + Port + " released");
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

            WriteResponse(stream, status, body);
        }
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
        return "{\"bridge_version\":\"" + BridgeVersion +
               "\",\"project\":\"" + JsonEscape(project) +
               "\",\"model_loaded\":" + Bool(modelLoaded) + "}";
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
    // The valid set the validity gate hands back on an unknown-property rejection.
    private string PropertyNamesJsonList(object node)
    {
        var sb = new StringBuilder();
        int count = 0;
        foreach (var pn in node.GetType()
                     .GetProperties(BindingFlags.Public | BindingFlags.Instance)
                     .Where(IsLegendProp)
                     .Select(pi => pi.Name).Distinct().OrderBy(x => x))
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
        if (node.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance)
                .Any(p => p.Name == name)) return null;    // type-declared -> safe
        return "{\"error\":{\"code\":\"unknown_property\",\"message\":\"" +
               JsonEscape(node.GetType().Name + " has no settable property '" + name +
                          "' (call describe_type/describe_node for the valid set)") +
               "\",\"valid_properties\":[" + PropertyNamesJsonList(node) + "]}}";
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
            default: return OpcUa.DataTypes.Boolean;
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
    private string SetPropertyInline(string firstLine)
    {
        string path = QueryParam(firstLine, "path");
        string name = QueryParam(firstLine, "name");
        string raw  = QueryParam(firstLine, "value") ?? "";
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(name))
            return ErrorJson("bad_query", "required query params: path, name");
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
    private static string SetEnumOrRaw(IUAVariable v, string dt, string raw)
    {
        int ord;
        if (int.TryParse(raw, out ord)) { v.Value = ord; return null; }
        if (TryEnumOrdinal(dt, raw, out ord)) { v.Value = ord; return null; }
        // Neither an ordinal nor a known member name. A bare-string assign to an enum
        // asserts (!localeId.empty) and leaks a cryptic native CoreException, so refuse
        // with a clean, actionable error instead of falling through to it.
        var known = KnownEnumMembers(dt);
        if (known != null)
            return "invalid value '" + raw + "' for enum " + dt + "; valid: " + string.Join(", ", known);
        // Genuinely exotic (non-enum) datatype: attempt the string assign, but catch the
        // native assert so an enum we don't have a member list for still gets a clean message.
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
    private static string[] KnownEnumMembers(string dt)
    {
        switch (dt)
        {
            case "HorizontalAlignment": return new[] { "Left", "Center", "Right", "Stretch" };
            case "VerticalAlignment":   return new[] { "Top", "Center", "Bottom", "Stretch" };
            default: return null;
        }
    }

    // Friendly member name -> ordinal for the built-in FTOptix.UI alignment enums.
    // Case-insensitive. Extend here as more enum properties are exercised.
    private static bool TryEnumOrdinal(string dt, string name, out int ord)
    {
        ord = 0;
        string key = (dt ?? "") + "." + (name ?? "").Trim().ToLowerInvariant();
        switch (key)
        {
            case "HorizontalAlignment.left":    ord = 0; return true;
            case "HorizontalAlignment.center":  ord = 1; return true;
            case "HorizontalAlignment.right":   ord = 2; return true;
            case "HorizontalAlignment.stretch": ord = 3; return true;
            case "VerticalAlignment.top":       ord = 0; return true;
            case "VerticalAlignment.center":    ord = 1; return true;
            case "VerticalAlignment.bottom":    ord = 2; return true;
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
                    // Value typed to the target variable's own DataType, coerced from raw.
                    var valVar = InformationModel.MakeVariable("Value", cmdTargetVar.DataType);
                    valVar.Value = CoerceRaw(DataTypeName(cmdTargetVar), cmdValueRaw, firstLine);
                    inputArgs.Add(valVar);
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

    // Structural validation of an ExpressionEvaluator formula. Optix exposes NO
    // design-time parser (confirmed by reflection: ExpressionEvaluator has only
    // Expression/ExpressionVariable + inherited Start/Stop) and validates a formula
    // only at RUNTIME (a bad one silently no-ops). This catches the common author-time
    // mistakes WITHOUT reimplementing the grammar: unbalanced ()/{}, out-of-range {N}
    // placeholders, unknown function names, unterminated strings. String literals are
    // skipped so parens/braces inside them don't false-positive. Returns null when the
    // formula is structurally sound, else a human-readable reason.
    private static string ValidateExpressionSyntax(string expr, int sourceCount)
    {
        if (string.IsNullOrWhiteSpace(expr)) return "expression is empty";
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

    private string ValueString(IUAVariable v)
    {
        try { return v.Value == null ? "null" : v.Value.ToString(); }
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
