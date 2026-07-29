// QwenMenu -- menu bar control for the Mac's qwen2.5-72b.
//
// Talks to the qwen-control agent over loopback (which is why it needs no token:
// the agent trusts 127.0.0.1, and only demands the shared bearer from tailnet
// callers). Pure AppKit, one file, built with swiftc -- no Xcode project, no
// SwiftUI, no dependencies to drift.
//
// Named main.swift on purpose: swiftc only allows top-level statements there.

import AppKit
import Foundation

let AGENT = "http://127.0.0.1:8121"
let REMOTE_URL = "https://pi.your-tailnet.ts.net:8463"
let POLL_SECONDS = 5.0

// MARK: - tiny JSON helpers

func dictOf(_ any: Any?) -> [String: Any] { (any as? [String: Any]) ?? [:] }
func boolOf(_ any: Any?) -> Bool { (any as? Bool) ?? false }
func intOf(_ any: Any?) -> Int {
    if let i = any as? Int { return i }
    if let d = any as? Double { return Int(d) }
    return 0
}
func strOf(_ any: Any?) -> String { (any as? String) ?? "" }

func humanBytes(_ b: Int) -> String {
    if b <= 0 { return "—" }
    let gb = Double(b) / 1_073_741_824.0
    if gb >= 1 { return String(format: "%.1f GB", gb) }
    return "\(b / 1_048_576) MB"
}

func humanMinutes(_ seconds: Int) -> String {
    if seconds <= 0 { return "—" }
    if seconds < 60 { return "\(seconds)s" }
    let m = seconds / 60
    return m < 60 ? "\(m)m" : String(format: "%dh %dm", m / 60, m % 60)
}

// MARK: - agent client

enum Agent {
    static func get(_ path: String, done: @escaping ([String: Any]?) -> Void) {
        request(path, method: "GET", body: nil, done: done)
    }

    static func post(_ path: String, body: [String: Any]? = nil,
                     done: @escaping ([String: Any]?) -> Void = { _ in }) {
        request(path, method: "POST", body: body, done: done)
    }

    private static func request(_ path: String, method: String, body: [String: Any]?,
                               done: @escaping ([String: Any]?) -> Void) {
        guard let url = URL(string: AGENT + path) else { done(nil); return }
        var req = URLRequest(url: url)
        req.httpMethod = method
        // Must stay above the agent's own worst case for /on: up to 60s waiting for
        // LocalAI's LaunchAgent to answer, plus a 180s warm-up timeout = 240s. 330
        // leaves margin; anything below 240 would time out here while the agent was
        // still legitimately working.
        req.timeoutInterval = 330
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body = body {
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var parsed: [String: Any]? = nil
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                parsed = obj
            }
            DispatchQueue.main.async { done(parsed) }
        }.resume()
    }
}

// MARK: - derived status

enum Power {
    case hot, ready, noServer, unknown

    var label: String {
        switch self {
        case .hot: return "HOT"
        case .ready: return "READY"
        case .noServer: return "NO SERVER"
        case .unknown: return "…"
        }
    }

    var colour: NSColor {
        switch self {
        case .hot: return NSColor.systemOrange
        case .ready: return NSColor.systemBlue
        case .noServer: return NSColor.systemRed
        case .unknown: return NSColor.secondaryLabelColor
        }
    }

    var glyph: String {
        switch self {
        case .hot: return "flame.fill"
        case .ready: return "moon.zzz.fill"
        case .noServer: return "exclamationmark.triangle.fill"
        case .unknown: return "circle.dotted"
        }
    }
}

func classify(_ s: [String: Any]?) -> Power {
    guard let s = s else { return .unknown }
    if !boolOf(s["localai_api"]) { return .noServer }
    if boolOf(s["model_resident"]) || boolOf(s["backend_alive"]) { return .hot }
    return .ready
}

// MARK: - app

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    let menu = NSMenu()
    var state: [String: Any]?
    var busy: String?
    var timer: Timer?

    // removeAllItems() on a menu the user currently has open drops the item they
    // were about to click and loses the highlight. The 5s poll must not do that, so
    // while the menu is open we keep updating `state` and the status bar glyph but
    // defer the menu rebuild until it closes.
    var menuIsOpen = false
    var rebuildDeferred = false

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.menu = menu
        menu.autoenablesItems = false
        menu.delegate = self
        rebuild()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: POLL_SECONDS, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        rebuild()          // one rebuild up front, while it is still safe
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
        if rebuildDeferred {
            rebuildDeferred = false
            rebuild()
        }
    }

    func refresh() {
        Agent.get("/state") { [weak self] s in
            guard let self = self else { return }
            if s != nil { self.state = s }
            if self.menuIsOpen {
                self.rebuildDeferred = true
                self.updateStatusButton()   // glyph and tooltip are safe to touch
            } else {
                self.rebuild()
            }
        }
    }

    // MARK: menu construction

    private func row(_ key: String, _ value: String) -> NSMenuItem {
        let m = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        m.isEnabled = false
        let para = NSMutableParagraphStyle()
        para.tabStops = [NSTextTab(textAlignment: .left, location: 96)]
        let text = NSMutableAttributedString(
            string: "\(key)\t\(value)",
            attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular),
                .paragraphStyle: para,
                .foregroundColor: NSColor.secondaryLabelColor,
            ])
        text.addAttribute(.foregroundColor, value: NSColor.labelColor,
                          range: NSRange(location: key.count + 1,
                                         length: text.length - key.count - 1))
        m.attributedTitle = text
        return m
    }

    private func warning(_ text: String) -> NSMenuItem {
        let m = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        m.isEnabled = false
        m.attributedTitle = NSAttributedString(
            string: text,
            attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                .foregroundColor: NSColor.systemOrange,
            ])
        if let img = NSImage(systemSymbolName: "battery.25", accessibilityDescription: nil) {
            m.image = img
        }
        return m
    }

    private func action(_ title: String, _ sel: Selector, symbol: String,
                        enabled: Bool = true, checked: Bool = false) -> NSMenuItem {
        let m = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        m.target = self
        m.isEnabled = enabled
        m.state = checked ? .on : .off
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
            m.image = img
        }
        return m
    }

    // Safe to call at any time, including while the menu is open.
    func updateStatusButton() {
        let power = classify(state)
        guard let button = item.button else { return }
        let cfg = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        let img = NSImage(systemSymbolName: power.glyph, accessibilityDescription: power.label)?
            .withSymbolConfiguration(cfg)
        img?.isTemplate = false
        button.image = img
        button.contentTintColor = power.colour
        var tip = "Qwen 2.5 72B — \(power.label)"
        if let ac = state?["on_ac_power"] as? Bool, !ac { tip += " (on battery)" }
        button.toolTip = tip
    }

    func rebuild() {
        let s = state
        let power = classify(s)
        let kw = dictOf(s?["keep_warm"])
        let counters = dictOf(s?["counters"])
        let working = busy != nil

        updateStatusButton()
        menu.removeAllItems()

        // header
        let header = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        header.isEnabled = false
        let head = NSMutableAttributedString(
            string: "Qwen 2.5 72B   ",
            attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .semibold)])
        head.append(NSAttributedString(
            string: working ? (busy ?? "").uppercased() : power.label,
            attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .bold),
                .foregroundColor: working ? NSColor.systemOrange : power.colour,
            ]))
        header.attributedTitle = head
        menu.addItem(header)
        menu.addItem(.separator())

        // stats
        if let s = s {
            menu.addItem(row("Model", boolOf(s["model_resident"]) || boolOf(s["backend_alive"])
                                       ? "resident" : "not loaded"))
            menu.addItem(row("Server", boolOf(s["localai_api"]) ? "up" : "not answering"))
            menu.addItem(row("GPU", humanBytes(intOf(s["gpu_bytes"]))))
            if boolOf(kw["active"]) {
                menu.addItem(row("Keep warm", humanMinutes(intOf(kw["seconds_left"])) + " left"))
            } else {
                menu.addItem(row("Keep warm", "off"))
            }
            menu.addItem(row("Awake hold", boolOf(s["caffeinate_held"]) ? "held" : "off"))
            if let ac = s["on_ac_power"] as? Bool {
                menu.addItem(row("Power", ac ? "AC" : "battery"))
            }
            // requests is null whenever LocalAI's /metrics could not be read. Show
            // that rather than a number: the old row added warm-ups to a counter that
            // was structurally always 0, so it was wrong in both directions.
            if counters["requests"] is NSNull || counters["requests"] == nil {
                menu.addItem(row("Requests", "unavailable"))
            } else {
                menu.addItem(row("Requests", "\(intOf(counters["requests"])) today"))
            }
        } else {
            menu.addItem(row("Agent", "not reachable"))
        }

        // Hazard, not decoration: on battery the awake hold cannot survive a lid
        // close, and if this Mac sleeps it leaves the tailnet and cannot be woken
        // back. Say so before the user relies on Keep Warm, not after.
        if let ac = s?["on_ac_power"] as? Bool, !ac {
            menu.addItem(warning("On battery — sleeps on lid close"))
        }
        menu.addItem(.separator())

        // actions
        let isHot = power == .hot
        menu.addItem(action(isHot ? "Turn Off" : "Turn On",
                            isHot ? #selector(doOff) : #selector(doOn),
                            symbol: "power", enabled: !working))
        menu.addItem(action("Warm Up Now", #selector(doWarm),
                            symbol: "flame", enabled: !working))
        menu.addItem(action("Keep Warm 30 min", #selector(toggleKeepWarm),
                            symbol: "thermometer.medium", enabled: !working,
                            checked: boolOf(kw["active"])))
        menu.addItem(action("Keep Mac Awake", #selector(toggleAwake),
                            symbol: "cup.and.saucer", enabled: !working,
                            checked: boolOf(s?["caffeinate_held"])))
        menu.addItem(.separator())
        menu.addItem(action("Open Remote Control…", #selector(openRemote), symbol: "globe"))

        // Destructive: booting LocalAI out is the one move that can strand the
        // service, so it lives behind its own confirm rather than the power item.
        let adv = NSMenu()
        let advItem = NSMenuItem(title: "Advanced", action: nil, keyEquivalent: "")
        advItem.image = NSImage(systemSymbolName: "wrench.and.screwdriver", accessibilityDescription: nil)
        adv.addItem(action("Start LocalAI Server", #selector(doServerStart), symbol: "play"))
        adv.addItem(action("Stop LocalAI Server…", #selector(doServerStop), symbol: "stop"))
        adv.addItem(action("Unload Model Now", #selector(doUnload), symbol: "eject"))
        advItem.submenu = adv
        menu.addItem(advItem)

        menu.addItem(.separator())
        menu.addItem(action("Quit QwenMenu", #selector(doQuit), symbol: "xmark.circle"))
    }

    // MARK: actions

    private func run(_ path: String, _ label: String, body: [String: Any]? = nil) {
        busy = label
        rebuild()
        Agent.post(path, body: body) { [weak self] resp in
            guard let self = self else { return }
            self.busy = nil
            if let st = resp?["state"] as? [String: Any] { self.state = st }
            self.rebuild()
            self.refresh()
        }
    }

    @objc func doOn() { run("/on", "warming", body: ["minutes": 30]) }
    @objc func doOff() { run("/off", "stopping") }
    @objc func doWarm() { run("/warm", "warming") }
    @objc func doUnload() { run("/unload", "unloading") }
    @objc func doServerStart() { run("/localai/start", "starting") }

    @objc func toggleKeepWarm() {
        let on = boolOf(dictOf(state?["keep_warm"])["active"])
        run(on ? "/keepwarm/off" : "/keepwarm/on", on ? "releasing" : "warming",
            body: on ? nil : ["minutes": 30])
    }

    @objc func toggleAwake() {
        let held = boolOf(state?["caffeinate_held"])
        run(held ? "/caffeinate/off" : "/caffeinate/on", held ? "releasing" : "holding")
    }

    @objc func doServerStop() {
        let a = NSAlert()
        a.messageText = "Stop the LocalAI server?"
        a.informativeText = """
            This boots the LaunchAgent out entirely. Nothing on the tailnet — \
            including the Pi's remote control page — can start the model again \
            until the server is running, and you would have to start it from \
            this Mac. Turning Qwen off does not require this.
            """
        a.alertStyle = .warning
        a.addButton(withTitle: "Stop Server")
        a.addButton(withTitle: "Cancel")
        if a.runModal() == .alertFirstButtonReturn {
            run("/localai/stop", "stopping")
        }
    }

    @objc func openRemote() {
        if let u = URL(string: REMOTE_URL) { NSWorkspace.shared.open(u) }
    }

    @objc func doQuit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // menu bar only: no Dock icon, no window
app.run()
