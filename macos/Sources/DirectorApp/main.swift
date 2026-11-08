import AppKit
import WebKit

final class DirectorApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var server: Process?
    var logHandle: FileHandle?
    var root: URL!
    var base: URL!
    var closing = false
    var starting = false
    var pickerOpen = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "AI Dev Director 종료…", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); menu.addItem(editItem)
        let edit = NSMenu(title: "편집"); editItem.submenu = edit
        for (title, selector, key) in [("실행 취소", "undo:", "z"), ("잘라내기", "cut:", "x"), ("복사", "copy:", "c"), ("붙여넣기", "paste:", "v"), ("전체 선택", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        let viewItem = NSMenuItem(); menu.addItem(viewItem)
        let view = NSMenu(title: "보기"); viewItem.submenu = view
        let reload = view.addItem(withTitle: "콘솔 다시 연결", action: #selector(reconnect), keyEquivalent: "r"); reload.target = self
        let folder = view.addItem(withTitle: "프로젝트 폴더 선택…", action: #selector(selectProject), keyEquivalent: "o"); folder.target = self
        NSApp.mainMenu = menu
        root = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "DirectorRoot") as? String ?? "")
        let configData = try? Data(contentsOf: root.appendingPathComponent("companion.json"))
        let config = configData.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let port = config?["port"] as? Int ?? 8081
        base = URL(string: "http://127.0.0.1:\(port)")!
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: min(1320, screen.width - 60), height: min(850, screen.height - 60)), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "AI Dev Director"
        window.minSize = NSSize(width: 800, height: 560)
        window.setFrameAutosaveName("DirectorMainWindow")
        window.center(); window.delegate = self
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "chooseFolder")
        let bridge = """
        (() => {
          const original = window.fetch.bind(window);
          let next = 0; const waiting = new Map();
          window.directorFolderResult = (id, path) => { const resolve = waiting.get(id); if (resolve) { waiting.delete(id); resolve(new Response(JSON.stringify({path}), {status:200, headers:{'Content-Type':'application/json'}})); } };
          window.fetch = (input, options) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.origin === location.origin && url.pathname === '/ops/pick-folder') {
              return new Promise(resolve => { const id=++next; waiting.set(id, resolve); window.webkit.messageHandlers.chooseFolder.postMessage(id); });
            }
            return original(input, options);
          };
        })();
        """
        configuration.userContentController.addUserScript(WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: window.contentView!.bounds, configuration: configuration)
        web.autoresizingMask = [.width, .height]; web.navigationDelegate = self; web.uiDelegate = self
        window.contentView!.addSubview(web)
        status = NSTextField(wrappingLabelWithString: "서버에 연결하고 있습니다…")
        status.font = .systemFont(ofSize: 16); status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(status)
        NSLayoutConstraint.activate([status.centerXAnchor.constraint(equalTo: window.contentView!.centerXAnchor), status.centerYAnchor.constraint(equalTo: window.contentView!.centerYAnchor), status.widthAnchor.constraint(lessThanOrEqualToConstant: 650)])
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        reconnect()
    }

    func local(_ url: URL?) -> Bool {
        guard let url = url else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }
    @objc func reconnect() {
        guard !starting else { return }
        starting = true; status.isHidden = false; status.stringValue = "서버에 연결하고 있습니다…"
        probe(remaining: 40, allowLaunch: true)
    }
    func probe(remaining: Int, allowLaunch: Bool) {
        var request = URLRequest(url: base.appendingPathComponent("ops/roles")); request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, response, error in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let valid = (response as? HTTPURLResponse)?.statusCode == 200 && json?["roles"] != nil && json?["csrf"] is String
            DispatchQueue.main.async {
                if valid {
                    self.starting = false
                    self.web.load(URLRequest(url: self.base)); return
                }
                if response != nil {
                    self.fail("설정된 포트에서 다른 응답이 왔습니다. 기존 서버와 companion.json의 포트를 확인해 주세요."); return
                }
                if allowLaunch && self.server?.isRunning != true {
                    do { try self.launchServer() } catch { self.fail("서버 시작 실패: \(error.localizedDescription)"); return }
                }
                guard remaining > 0 else { self.fail("서버에 연결하지 못했습니다. data/desktop-server.log를 확인한 뒤 보기 → 콘솔 다시 연결을 선택하세요."); return }
                if let process = self.server, !process.isRunning {
                    self.fail("서버가 종료됐습니다. data/desktop-server.log를 확인해 주세요."); return
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.probe(remaining: remaining - 1, allowLaunch: false) }
            }
        }.resume()
    }
    func fail(_ message: String) { starting = false; status.isHidden = false; status.stringValue = message }
    func launchServer() throws {
        let process = Process()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.arguments = ["-u", "companion_server.py"]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        environment["PATH"] = "\(root.path)/.venv/bin:\(home)/.local/bin:/Applications/Gemini.app/Contents/Helpers:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        let logs = root.appendingPathComponent("data/desktop-server.log")
        if !FileManager.default.fileExists(atPath: logs.path) { FileManager.default.createFile(atPath: logs.path, contents: nil) }
        logHandle = try FileHandle(forWritingTo: logs); try logHandle?.seekToEnd()
        process.standardOutput = logHandle; process.standardError = logHandle; process.standardInput = FileHandle.nullDevice
        try process.run(); server = process
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { status.isHidden = true }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { fail("연결 오류: \(error.localizedDescription)\n보기 → 콘솔 다시 연결을 선택하세요.") }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if local(navigationAction.request.url) { decisionHandler(.allow) }
        else {
            if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, local(message.frameInfo.request.url), let id = message.body as? Int else { return }
        chooseFolder { path in
            let encoded = String(data: try! JSONSerialization.data(withJSONObject: [id, path]), encoding: .utf8)!
            self.web.evaluateJavaScript("window.directorFolderResult(...\(encoded))", completionHandler: nil)
        }
    }
    func chooseFolder(completion: @escaping (String) -> Void) {
        guard !pickerOpen else { completion(""); return }
        pickerOpen = true
        let panel = NSOpenPanel(); panel.title = "작업할 프로젝트 폴더 선택"
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { response in
            self.pickerOpen = false
            completion(response == .OK ? panel.url?.path ?? "" : "")
        }
    }
    @objc func selectProject() {
        guard local(web.url), !starting else { return }
        chooseFolder { path in
            guard !path.isEmpty else { return }
            let encoded = String(data: try! JSONSerialization.data(withJSONObject: [path]), encoding: .utf8)!
            self.web.evaluateJavaScript("fetch('/project',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:new URLSearchParams({path:\(encoded)[0]}).toString()}).then(r=>r.json()).then(r=>{if(r.ok)location.reload();else alert(r.message||'프로젝트 선택 실패');})", completionHandler: nil)
        }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "확인")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "확인"); alert.addButton(withTitle: "취소")
        alert.beginSheetModal(for: window) { response in completionHandler(response == .alertFirstButtonReturn) }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { window.makeKeyAndOrderFront(nil); return true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if closing { return .terminateNow }
        let alert = NSAlert(); alert.messageText = "AI Dev Director를 종료할까요?"
        alert.informativeText = "백그라운드 계속 실행은 창만 닫고 서버와 작업을 유지합니다. 작업 중지 후 종료는 진행 중인 미션을 멈추고 서버도 종료합니다."
        alert.addButton(withTitle: "백그라운드 계속 실행"); alert.addButton(withTitle: "작업 중지 후 종료"); alert.addButton(withTitle: "취소")
        let answer = alert.runModal()
        if answer == .alertFirstButtonReturn { return .terminateNow }
        if answer != .alertSecondButtonReturn { return .terminateCancel }
        stopServer()
        return .terminateLater
    }
    func post(_ path: String, completion: @escaping (Bool) -> Void) {
        guard let token = try? String(contentsOf: root.appendingPathComponent("data/solomon-bridge.token"), encoding: .utf8) else { completion(false); return }
        var request = URLRequest(url: base.appendingPathComponent(path)); request.httpMethod = "POST"; request.timeoutInterval = 20
        request.setValue(token.trimmingCharacters(in: .whitespacesAndNewlines), forHTTPHeaderField: "X-Solomon-Bridge")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = Data("{}".utf8)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            completion((response as? HTTPURLResponse)?.statusCode == 200)
        }.resume()
    }
    func stopServer() {
        post("ops/missions/stop") { ok in
            guard ok else { DispatchQueue.main.async { self.fail("작업 중지를 확인하지 못했습니다. 콘솔에서 상태를 확인해 주세요."); NSApp.reply(toApplicationShouldTerminate: false) }; return }
            self.post("ops/shutdown") { stopped in DispatchQueue.main.async {
                self.closing = stopped
                if !stopped { self.fail("서버 종료를 확인하지 못했습니다.") }
                NSApp.reply(toApplicationShouldTerminate: stopped)
            }}
        }
    }
}
let app = NSApplication.shared
let delegate = DirectorApp()
app.delegate = delegate
app.run()
