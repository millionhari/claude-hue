// Claude Hue.app — native window shell.
//
// AppKit + WKWebView around the local dashboard: a real app window with a Dock
// icon and a menu bar, not a browser tab. On launch it runs the Python entry
// point in --install-only mode (hooks + payload into ~/.claude/hue_hooks),
// starts dashboard.py if nothing is already serving the port, and loads it.
//
// Compiled by installer/build.sh with swiftc — no third-party frameworks.

import AppKit
import WebKit

let kPort = 8420
let kBase = URL(string: "http://127.0.0.1:\(kPort)/")!
let kStatus = URL(string: "http://127.0.0.1:\(kPort)/api/status")!
let kServerLog = "/tmp/claude_hue_dashboard.log"
let kCream = NSColor(srgbRed: 0.992, green: 0.953, blue: 0.890, alpha: 1)  // #fdf3e3

// MARK: - subprocess helpers

@discardableResult
func runCapture(_ exe: String, _ args: [String]) -> (status: Int32, out: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "\(error)") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

/// macOS ships no usable python3 of its own: /usr/bin/python3 is a stub that
/// pops the Command Line Tools installer when they are missing, and a GUI app's
/// PATH never includes Homebrew. So probe known locations and prove each one
/// runs before using it.
func findPython() -> String? {
    var candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                      "/Library/Developer/CommandLineTools/usr/bin/python3"]
    // /usr/bin/python3 only once we know the developer tools are actually there.
    let (xsel, xpath) = runCapture("/usr/bin/xcode-select", ["-p"])
    if xsel == 0, FileManager.default.fileExists(
        atPath: xpath.trimmingCharacters(in: .whitespacesAndNewlines)) {
        candidates.append("/usr/bin/python3")
    }
    for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
        let (code, out) = runCapture(path, ["-c", "print(1)"])
        if code == 0, out.contains("1") { return path }
    }
    return nil
}

func serverUp() -> Bool {
    var req = URLRequest(url: kStatus)
    req.timeoutInterval = 1.5
    req.cachePolicy = .reloadIgnoringLocalCacheData
    var ok = false
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        ok = (resp as? HTTPURLResponse)?.statusCode == 200
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 3)
    return ok
}

func postShutdown() {
    var req = URLRequest(url: URL(string: "http://127.0.0.1:\(kPort)/api/shutdown")!)
    req.httpMethod = "POST"
    req.timeoutInterval = 1.5
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
    _ = sem.wait(timeout: .now() + 2)
}

// MARK: - app

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                         WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?          // only set when *we* started the dashboard

    // Where the app installs its payload; dashboard.py is run from there so a
    // running server never depends on the .app staying mounted.
    let dashDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".claude/hue_hooks/dashboard")

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildWindow()
        showSplash("Starting Claude Hue…", detail: "installing hooks and dashboard")
        DispatchQueue.global(qos: .userInitiated).async { self.bootstrap() }
    }

    // MARK: window + menu

    func buildWindow() {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "hueShell")
        // Appended to the stock user agent — how the page tells it is hosted in
        // the app rather than in a browser.
        cfg.applicationNameForUserAgent = "ClaudeHueShell/1.0"
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        if #available(macOS 12.0, *) { web.underPageBackgroundColor = kCream }
        if #available(macOS 13.3, *) { web.isInspectable = true }   // right-click → Inspect

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Claude Hue"
        window.backgroundColor = kCream
        window.minSize = NSSize(width: 760, height: 560)
        window.contentView = web
        window.setFrameAutosaveName("ClaudeHueMain")
        if window.frame.origin == .zero { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Claude Hue",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Reload Dashboard", action: #selector(reload), keyEquivalent: "r")
        appMenu.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "")
        appMenu.addItem(withTitle: "Show Server Log", action: #selector(showLog), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Claude Hue", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Claude Hue", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        // Standard responders — without an Edit menu the web view gets no ⌘C/⌘V.
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Actual Size", action: #selector(zoomReset), keyEquivalent: "0")
        view.addItem(withTitle: "Zoom In", action: #selector(zoomIn), keyEquivalent: "+")
        view.addItem(withTitle: "Zoom Out", action: #selector(zoomOut), keyEquivalent: "-")
        viewItem.submenu = view
        main.addItem(viewItem)

        let winItem = NSMenuItem()
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        main.addItem(winItem)

        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }

    @objc func reload() { load() }
    @objc func openInBrowser() { NSWorkspace.shared.open(kBase) }
    @objc func showLog() {
        NSWorkspace.shared.selectFile(kServerLog, inFileViewerRootedAtPath: "/tmp")
    }
    @objc func zoomIn() { web.pageZoom = min(web.pageZoom + 0.1, 2.5) }
    @objc func zoomOut() { web.pageZoom = max(web.pageZoom - 0.1, 0.6) }
    @objc func zoomReset() { web.pageZoom = 1 }

    // MARK: startup

    func bootstrap() {
        guard let python = findPython() else {
            return splashOnMain("Python 3 is required",
                                detail: "Claude Hue runs a small stdlib-only Python server.<br>"
                                      + "Install it with <code>brew install python</code> or "
                                      + "<code>xcode-select --install</code>, then reopen the app.",
                                retry: true)
        }
        // Idempotent: refreshes the payload and wires Claude Code's hooks.
        if let entry = Bundle.main.resourceURL?.appendingPathComponent("claude_hue_app.py").path,
           FileManager.default.fileExists(atPath: entry) {
            let (code, out) = runCapture(python, [entry, "--install-only"])
            if code != 0 {
                return splashOnMain("Install step failed",
                                    detail: "<pre>\(escape(out))</pre>", retry: true)
            }
        }
        if !serverUp() { startServer(python) }
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            if serverUp() { return DispatchQueue.main.async { self.load() } }
            Thread.sleep(forTimeInterval: 0.2)
        }
        splashOnMain("Dashboard did not start",
                     detail: "Nothing is answering on port \(kPort). "
                           + "See <code>\(kServerLog)</code>.", retry: true)
    }

    func startServer(_ python: String) {
        let script = dashDir.appendingPathComponent("dashboard.py")
        guard FileManager.default.fileExists(atPath: script.path) else { return }
        if !FileManager.default.fileExists(atPath: kServerLog) {
            FileManager.default.createFile(atPath: kServerLog, contents: nil)
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script.path]
        if let log = FileHandle(forWritingAtPath: kServerLog) {
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
        }
        do { try p.run(); server = p } catch { server = nil }
    }

    func load() {
        web.load(URLRequest(url: kBase, cachePolicy: .reloadIgnoringLocalCacheData,
                            timeoutInterval: 15))
    }

    // MARK: splash / error screen

    func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
         .replacingOccurrences(of: "<", with: "&lt;")
         .replacingOccurrences(of: ">", with: "&gt;")
    }

    func splashOnMain(_ title: String, detail: String, retry: Bool = false) {
        DispatchQueue.main.async { self.showSplash(title, detail: detail, retry: retry) }
    }

    func showSplash(_ title: String, detail: String, retry: Bool = false) {
        let button = retry
            ? "<button onclick=\"webkit.messageHandlers.hueShell.postMessage('retry')\">try again</button>"
            : "<div class=dots><i></i><i></i><i></i></div>"
        web.loadHTMLString("""
        <!doctype html><meta charset=utf-8><style>
          :root { color-scheme: light }
          body { margin:0; height:100vh; display:grid; place-content:center; gap:14px;
                 text-align:center; padding:40px;
                 background:#fdf3e3 radial-gradient(rgba(33,26,21,.06) 1.5px, transparent 1.5px);
                 background-size:26px 26px; color:#211a15;
                 font:500 15px/1.55 "Avenir Next",system-ui,sans-serif }
          h1 { font-size:19px; letter-spacing:-.01em }
          p, pre { color:#5c4f44; font-size:13.5px; margin:0; max-width:44ch }
          pre { text-align:left; white-space:pre-wrap; overflow:auto; max-height:38vh;
                font:400 12px/1.5 ui-monospace,monospace }
          code { font:400 12.5px ui-monospace,monospace; background:rgba(33,26,21,.07);
                 padding:1px 5px; border-radius:5px }
          button { justify-self:center; font:600 13px "Avenir Next",system-ui,sans-serif;
                   padding:7px 16px; border-radius:8px; border:1px solid rgba(33,26,21,.18);
                   background:#fffdf9; color:#211a15; cursor:pointer }
          .dots { display:flex; gap:7px; justify-content:center }
          .dots i { width:7px; height:7px; border-radius:50%; background:#e8a33d;
                    animation:b 1.1s infinite ease-in-out }
          .dots i:nth-child(2){animation-delay:.16s} .dots i:nth-child(3){animation-delay:.32s}
          @keyframes b { 0%,80%,100%{opacity:.25;transform:translateY(0)}
                         40%{opacity:1;transform:translateY(-4px)} }
        </style>
        <h1>\(escape(title))</h1><p>\(detail)</p>\(button)
        """, baseURL: nil)
    }

    // MARK: delegates

    func userContentController(_ c: WKUserContentController, didReceive msg: WKScriptMessage) {
        switch msg.body as? String {
        case "quit": NSApp.terminate(nil)
        case "retry":
            showSplash("Starting Claude Hue…", detail: "installing hooks and dashboard")
            DispatchQueue.global(qos: .userInitiated).async { self.bootstrap() }
        default: break
        }
    }

    // Keep the window on the dashboard; anything off-host opens in the browser.
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard action.targetFrame?.isMainFrame ?? true, let url = action.request.url else {
            return decisionHandler(.allow)
        }
        if url.host == "127.0.0.1" || url.host == "localhost" || url.scheme == "about" {
            return decisionHandler(.allow)
        }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    func webView(_ w: WKWebView, createWebViewWith cfg: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ w: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
        showSplash("Could not load the dashboard", detail: escape(error.localizedDescription), retry: true)
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        showSplash("Could not load the dashboard", detail: escape(error.localizedDescription), retry: true)
    }

    // MARK: lifecycle

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows visible: Bool) -> Bool {
        if !visible { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        // Only tear down a server this app started; one that was already running
        // belongs to whoever launched it.
        guard let server = server, server.isRunning else { return }
        postShutdown()
        for _ in 0..<20 where server.isRunning { Thread.sleep(forTimeInterval: 0.05) }
        if server.isRunning { server.terminate() }
    }
}

let delegate = AppDelegate()
NSApplication.shared.setActivationPolicy(.regular)
NSApplication.shared.delegate = delegate
NSApplication.shared.run()
