import AppKit
import WebKit
import Carbon

// A native shell around the existing loopback-only Agent Board, not a second board.
final class DesktopDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var statusItem: NSStatusItem!
    var backend: Process?
    var output: Pipe?
    var boardURL: URL?
    var pinItem: NSMenuItem!
    var stopping = false
    var generation = 0
    var pinned = UserDefaults.standard.object(forKey: "pinned") as? Bool ?? true

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let menu = NSMenu()
        func item(_ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem {
            let result = NSMenuItem(title: title, action: action, keyEquivalent: key)
            result.target = self
            menu.addItem(result)
            return result
        }
        _ = item("Show / Hide Agent Board", #selector(toggleWindow), "b")
        pinItem = item("Always on Top", #selector(togglePin))
        _ = item("Show / Hide Spaces", #selector(toggleSpaces))
        _ = item("Reload Board", #selector(reloadBoard), "r")
        _ = item("Restart Connection", #selector(restartBackend))
        _ = item("Enable Terminal Access…", #selector(requestTerminalAccess))
        _ = item("Open in Browser", #selector(openBrowser))
        menu.addItem(.separator())
        _ = item("Quit Agent Board", #selector(quit), "q")
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "rectangle.grid.1x2", accessibilityDescription: "Agent Board")
        statusItem.menu = menu
        let mainMenu = NSMenu()
        let application = NSMenuItem()
        application.submenu = menu.copy() as? NSMenu
        mainMenu.addItem(application)
        let edit = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        for (title, selector, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        edit.submenu = editMenu
        mainMenu.addItem(edit)
        NSApp.mainMenu = mainMenu

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 560),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Agent Board"
        window.minSize = NSSize(width: 380, height: 380)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        if !window.setFrameUsingName("AgentBoardDesktop") {
            if let visible = NSScreen.main?.visibleFrame {
                let size = NSSize(width: min(420, visible.width), height: min(560, visible.height - 24))
                window.setFrame(NSRect(x: visible.maxX - size.width - 16, y: visible.maxY - size.height - 16, width: size.width, height: size.height), display: false)
            }
        }
        // Recover saved positions when an external monitor was disconnected.
        if !NSScreen.screens.contains(where: { $0.visibleFrame.intersects(window.frame) }) { window.center() }
        window.setFrameAutosaveName("AgentBoardDesktop")
        if !UserDefaults.standard.bool(forKey: "compactListLayoutV2") {
            let old = window.frame
            window.setFrame(NSRect(x: old.maxX - 420, y: old.maxY - 560, width: 420, height: 560), display: false)
            window.saveFrame(usingName: "AgentBoardDesktop")
            UserDefaults.standard.set(true, forKey: "compactListLayoutV2")
        }
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window.contentView = webView
        applyPin()
        showWindow()
        startBackend()
    }

    func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func toggleWindow() { if window.isVisible { window.orderOut(nil) } else { showWindow() } }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applyPin() { window.level = pinned ? .floating : .normal; pinItem.state = pinned ? .on : .off }
    @objc func togglePin() { pinned.toggle(); UserDefaults.standard.set(pinned, forKey: "pinned"); applyPin() }
    @objc func toggleSpaces() { webView.evaluateJavaScript("document.documentElement.classList.toggle('desktop-show-sidebar')", completionHandler: nil) }
    @objc func reloadBoard() { if let url = boardURL { webView.load(URLRequest(url: url)) } else { restartBackend() } }
    @objc func openBrowser() { if let url = boardURL { var c = URLComponents(url: url, resolvingAgainstBaseURL: false)!; c.query = nil; if let fullURL = c.url { NSWorkspace.shared.open(fullURL) } } }
    @objc func requestTerminalAccess() {
        // Ask through the native app so macOS records the permission under Agent Board.
        DispatchQueue.global(qos: .userInitiated).async {
            let target = NSAppleEventDescriptor(bundleIdentifier: "com.apple.Terminal")
            let result = AEDeterminePermissionToAutomateTarget(target.aeDesc, AEEventClass(typeWildCard), AEEventID(typeWildCard), true)
            DispatchQueue.main.async {
                let alert = NSAlert()
                alert.messageText = result == noErr ? "Terminal access enabled" : "Terminal access needs permission"
                alert.informativeText = result == noErr ? "Click any thread to open its terminal." : "Enable Agent Board → Terminal in System Settings → Privacy & Security → Automation."
                alert.beginSheetModal(for: self.window)
            }
        }
    }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func restartBackend() { stopBackend(); startBackend() }
    func stopBackend() {
        generation += 1
        output?.fileHandleForReading.readabilityHandler = nil
        if let process = backend, process.isRunning { process.terminate(); process.waitUntilExit() }
        backend = nil
        output = nil
        boardURL = nil
    }
    func applicationWillTerminate(_ notification: Notification) { stopping = true; stopBackend() }

    func message(_ detail: String) {
        let safe = detail.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("<meta name='viewport' content='width=device-width'><style>body{font:15px -apple-system;padding:28px;color:#ddd;background:#20242c}h2{font-size:22px}p{line-height:1.6}</style><h2>Agent Board</h2><p>\(safe)</p><p>Use the Agent Board menu-bar icon to restart the connection or quit.</p>", baseURL: nil)
    }
    func startBackend() {
        message("Connecting to your open terminals…")
        guard let python = Bundle.main.object(forInfoDictionaryKey: "AgentBoardPython") as? String,
              let repo = Bundle.main.object(forInfoDictionaryKey: "AgentBoardRepository") as? String else {
            message("The app configuration is missing. Run scripts/install_desktop.py again."); return
        }
        generation += 1
        let current = generation
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-u", "-m", "claude_browse.board.desktop"]
        process.currentDirectoryURL = URL(fileURLWithPath: repo)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = repo
        environment["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        let logs = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Logs/Agent Board")
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let log = logs.appendingPathComponent("desktop.log")
        if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
        if let handle = try? FileHandle(forWritingTo: log) { handle.seekToEndOfFile(); process.standardError = handle }
        var buffer = Data()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            buffer.append(data)
            guard let text = String(data: buffer, encoding: .utf8), let line = text.split(separator: "\n").first,
                  text.contains("\n"), let url = URL(string: String(line)), url.scheme == "http", url.host == "127.0.0.1", url.port != nil else { return }
            handle.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self = self, self.generation == current else { return }
                var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
                components.queryItems = [URLQueryItem(name: "desktop", value: "1")]
                self.boardURL = components.url
                self.webView.load(URLRequest(url: self.boardURL!))
            }
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping, self.generation == current else { return }
                self.boardURL = nil
                self.message("The local connection stopped. Restart Connection from the menu bar. Details are in ~/Library/Logs/Agent Board/desktop.log.")
            }
        }
        backend = process
        output = pipe
        do { try process.run() } catch { message("Could not start Agent Board: \(error.localizedDescription)") }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" { decisionHandler(.allow); return }
        if let base = boardURL, url.scheme == base.scheme, url.host == base.host, url.port == base.port {
            decisionHandler(.allow)
        } else {
            // Only user-initiated web links leave the local app; never execute arbitrary schemes.
            if action.navigationType == .linkActivated, ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { message("Could not load the board. Restart Connection from the menu bar.") }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "OK"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
}
let app = NSApplication.shared
let delegate = DesktopDelegate()
app.delegate = delegate
app.run()
