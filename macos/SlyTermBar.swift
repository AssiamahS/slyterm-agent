import AppKit
import WebKit
import ServiceManagement

let SLYTERM = ("~/.local/bin/slyterm" as NSString).expandingTildeInPath
let CLAUDE = ("~/.local/bin/claude" as NSString).expandingTildeInPath
let HOME = NSHomeDirectory()
let REPO = ("~/slyterm" as NSString).expandingTildeInPath
let TTYD_PORT = 7699

// MARK: - Live terminal panel (ttyd + WKWebView)

// xterm.js keeps its selection outside the DOM and its Cmd+C path uses
// navigator.clipboard, which WKWebView denies — so copy is handled natively:
// grab term.getSelection() from ttyd's page and write NSPasteboard ourselves.
final class TerminalWebView: WKWebView {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
           event.charactersIgnoringModifiers == "c" {
            evaluateJavaScript("(window.term && term.getSelection()) || ''") { result, _ in
                if let sel = result as? String, !sel.isEmpty {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(sel, forType: .string)
                }
            }
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

final class TerminalViewController: NSViewController, WKNavigationDelegate, WKUIDelegate {
    var webView: TerminalWebView!
    var loaded = false

    override func loadView() {
        let cfg = WKWebViewConfiguration()
        webView = TerminalWebView(frame: NSRect(x: 0, y: 0, width: 640, height: 460), configuration: cfg)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        self.view = webView
    }

    // ttyd is localhost-only; grant mic/camera requests from the page so
    // voice input inside the popover works instead of being silently denied.
    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" ? .grant : .deny)
    }

    func connect() {
        guard !loaded else { return }
        loaded = true
        webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(TTYD_PORT)/")!))
    }

    func reload() {
        loaded = true
        webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(TTYD_PORT)/")!))
    }

    func webView(_ wv: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retryLater()
    }
    func webView(_ wv: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        retryLater()
    }
    private func retryLater() {
        // ttyd may still be starting up on first open
        loaded = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak self] in self?.connect() }
    }
}

// MARK: - App delegate

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    let terminal = TerminalViewController()
    var rightMenu: NSMenu!
    var ttyd: Process?

    func applicationDidFinishLaunching(_ note: Notification) {
        installEditMenu()
        startTtyd()

        popover.contentViewController = terminal
        popover.contentSize = NSSize(width: 640, height: 460)
        popover.behavior = .transient

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let btn = statusItem.button {
            btn.image = menuBarLogo()
            btn.toolTip = "Claude Code"
            btn.action = #selector(statusClicked)
            btn.target = self
            btn.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }

        let menu = NSMenu()
        menu.addItem(makeItem("New Session (reconnect)", #selector(menuNewSession), "n"))
        menu.addItem(makeItem("Restart Claude Backend", #selector(menuRestartBackend), "r"))
        menu.addItem(.separator())
        menu.addItem(makeItem("Open slyterm in Terminal App", #selector(openTerminal), "t"))
        menu.addItem(makeItem("Open slyterm Folder", #selector(openFolder), ""))
        let login = makeItem("Start at Login", #selector(toggleLogin), "")
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        menu.addItem(makeItem("Close slyterm Bar", #selector(NSApplication.terminate(_:)), "q"))
        rightMenu = menu
    }

    func applicationWillTerminate(_ note: Notification) {
        ttyd?.terminate()
    }

    // Accessory apps have no menu bar, so Cmd+C/V/X/A key equivalents go nowhere
    // unless a main menu with an Edit menu exists to route them down the responder chain.
    func installEditMenu() {
        let main = NSMenu()
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main
    }

    // Menu bar icon: bundled logo.png, rounded, 18pt
    func menuBarLogo() -> NSImage? {
        guard let path = Bundle.main.path(forResource: "logo", ofType: "png"),
              let src = NSImage(contentsOfFile: path) else {
            return NSImage(systemSymbolName: "terminal.fill", accessibilityDescription: "slyterm")
        }
        let size = NSSize(width: 18, height: 18)
        let img = NSImage(size: size, flipped: false) { rect in
            NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4).addClip()
            src.draw(in: rect)
            return true
        }
        img.isTemplate = false
        return img
    }

    func startTtyd() {
        ttyd?.terminate()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", """
        exec /opt/homebrew/bin/ttyd -p \(TTYD_PORT) -i 127.0.0.1 -W \
          -t disableLeaveAlert=true -t fontSize=13 -t cursorBlink=true \
          -t 'theme={"background":"#1d1f21"}' \
          \(CLAUDE)
        """]
        p.currentDirectoryURL = URL(fileURLWithPath: HOME)
        do { try p.run(); ttyd = p } catch { NSSound.beep() }
    }

    func makeItem(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    @objc func statusClicked() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            statusItem.menu = rightMenu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        if popover.isShown {
            popover.performClose(nil)
        } else if let btn = statusItem.button {
            popover.show(relativeTo: btn.bounds, of: btn, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
            terminal.connect()
            terminal.view.window?.makeFirstResponder(terminal.webView)
        }
    }

    @objc func menuNewSession() { terminal.reload() }

    @objc func menuRestartBackend() {
        startTtyd()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.terminal.reload() }
    }

    @objc func openTerminal() {
        let script = """
        tell application "Terminal"
            activate
            do script "cd \(REPO) && \(SLYTERM)"
        end tell
        """
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        task.arguments = ["-e", script]
        try? task.run()
    }

    @objc func openFolder() {
        NSWorkspace.shared.open(URL(fileURLWithPath: REPO))
    }

    @objc func toggleLogin(_ sender: NSMenuItem) {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
                sender.state = .off
            } else {
                try SMAppService.mainApp.register()
                sender.state = .on
            }
        } catch {
            NSSound.beep()
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
