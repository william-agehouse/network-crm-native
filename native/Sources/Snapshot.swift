import AppKit
import SwiftUI

// --snapshot <dir>: draws each screen offscreen with live engine data and saves
// <dir>/<scene>-<light|dark>.png. It needs no screen-recording permission and shows no
// window: the window sits far off-screen and the view draws itself into a bitmap.
//
// Scenes are set up ONLY through AppModel properties, so when other files replace the
// stub screens this file keeps working unchanged. A scene is captured once the screen
// calls `app.markSettled()` (or after 12 s), plus a second for layout and photos.
//
// Refuses to run without NC_NATIVE_PORT: a snapshot must never start or touch the real
// engine on 8765.

@MainActor
enum Snapshot {
    static let size = NSSize(width: 1280, height: 800)
    static let settleCap: TimeInterval = 12

    struct Scene {
        let name: String
        /// Port to point at; nil = NC_NATIVE_PORT.
        var port: Int? = nil
        /// nil = the whole window (RootView). Otherwise just this view, connected to the
        /// engine first: a sheet is its own window, which a picture of the main window
        /// cannot show, so sheets are drawn this way.
        var view: (@MainActor (AppModel) -> AnyView)? = nil
        /// For `view` scenes; nil = the window's size.
        var size: NSSize? = nil
        let setup: @MainActor (AppModel) async -> Void
    }

    static func run(directory: String) async -> Int32 {
        guard !directory.isEmpty else {
            print("usage: --snapshot <folder>")
            return 2
        }
        guard let raw = ProcessInfo.processInfo.environment["NC_NATIVE_PORT"], let port = Int(raw) else {
            print("REFUSED: set NC_NATIVE_PORT to the engine to draw from (the snapshot never uses the real 8765).")
            return 2
        }
        let dir = URL(fileURLWithPath: directory, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        // The first person by closeness, for the person scene.
        let probe = EngineClient(port: port)
        let firstID = try? await probe.get(
            "/api/contacts", query: ["sort": "closeness", "limit": "1"], as: ContactsResponse.self
        ).contacts.first?.id

        var scenes: [Scene] = [
            Scene(name: "people") { $0.section = .people },
            Scene(name: "people-search") { $0.section = .people; $0.peopleSearch = "an" },
            Scene(name: "person") { app in
                app.section = .people
                app.selectedPersonID = firstID
            },
            Scene(name: "ask") { $0.section = .ask },
            Scene(name: "lists") { $0.section = .lists },
            Scene(name: "data") { $0.section = .data },
            Scene(name: "settings") { $0.section = .settings },
            // Port 1 is never an engine: this draws the "cannot reach it" state.
            Scene(name: "engine-failed", port: 1) { $0.section = .people },
        ]
        // Each screen's own extra scenes, from its own file (Snapshot+<Screen>.swift).
        scenes += dataSettingsScenes(firstID: firstID) + setupScenes(firstID: firstID)
            + commandScenes(firstID: firstID) + peopleScenes(firstID: firstID) + askListsScenes(firstID: firstID)
            + engineScenes()

        // NC_SNAPSHOT_ONLY=people,person draws just those scenes (for a quick look at one).
        let only = Set((ProcessInfo.processInfo.environment["NC_SNAPSHOT_ONLY"] ?? "")
            .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) })
        let wanted = { (name: String) in only.isEmpty || only.contains(name) }

        var failures = 0
        for scene in scenes where wanted(scene.name) {
            for dark in [false, true] {
                let engine = EngineController(port: scene.port ?? port, isDevelopmentPort: true)
                let app = AppModel(engine: engine)
                let file = dir.appending(path: "\(scene.name)-\(dark ? "dark" : "light").png")
                let ok: Bool
                if let makeView = scene.view {
                    await engine.connect()
                    if engine.isReady { await app.refreshCounts() }
                    await scene.setup(app)
                    ok = await render(makeView(app).environment(app), size: scene.size ?? size, dark: dark, file: file) {
                        app.isSettled
                    }
                } else {
                    await scene.setup(app)
                    ok = await render(RootView().environment(app), size: size, dark: dark, file: file) { app.isSettled }
                }
                app.stopPolling()
                report(ok, file, settled: app.isSettled)
                if !ok { failures += 1 }
            }
        }
        return failures == 0 ? 0 : 1
    }

    private static func report(_ ok: Bool, _ file: URL, settled: Bool) {
        print("\(ok ? "SAVED" : "FAILED") \(file.lastPathComponent)\(settled ? "" : " (not settled after \(Int(settleCap)) s)")")
    }

    /// `view` in a window far off-screen, sized like the real one: what every picture and
    /// the self-test's table checks draw into. Not shown until the caller orders it front,
    /// and even then invisible, unclickable and never moved onto a screen.
    static func offscreenWindow<V: View>(_ view: V, size: NSSize) -> (NSWindow, NSHostingView<V>) {
        let hosting = NSHostingView(rootView: view)
        // The view never resizes the window: a resize lets AppKit move it onto the screen.
        hosting.sizingOptions = []
        // Put the screens' toolbars into the window, as WindowGroup does for the real app,
        // so content lays out below them instead of under an invisible toolbar's fade.
        hosting.sceneBridgingOptions = [.toolbars, .title]
        hosting.frame = NSRect(origin: .zero, size: size)
        let window = OffscreenWindow(
            contentRect: NSRect(origin: NSPoint(x: -4000, y: -4000), size: size),
            styleMask: [.titled, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        // Invisible and untouchable even if something did move it: a picture is drawn from
        // the views themselves (cacheDisplay), not from what the screen shows.
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.isExcludedFromWindowsMenu = true
        window.contentView = hosting
        window.setFrameOrigin(NSPoint(x: -4000, y: -4000))
        return (window, hosting)
    }

    /// Hosts `view` in an off-screen window, waits for `settled`, and writes a PNG.
    static func render<V: View>(
        _ view: V, size: NSSize, dark: Bool, file: URL, settled: @MainActor () -> Bool
    ) async -> Bool {
        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let (window, hosting) = offscreenWindow(view.environment(\.colorScheme, dark ? .dark : .light), size: size)
        window.appearance = appearance
        hosting.appearance = appearance
        // cacheDisplay resolves some AppKit-drawn colours (the sidebar's labels) against the
        // APPLICATION's appearance rather than the window's, so set both.
        NSApp.appearance = appearance
        window.orderFrontRegardless()

        let deadline = Date().addingTimeInterval(settleCap)
        while !settled() && Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        // Layout, images and the sidebar's counts land a moment after the first content.
        try? await Task.sleep(for: .seconds(1))
        hosting.layoutSubtreeIfNeeded()

        defer {
            window.orderOut(nil)
            window.contentView = nil
            window.close()
        }
        // The whole window, title bar and toolbar included: they live in the frame view
        // above the hosting view.
        let frameView = hosting.superview ?? hosting
        // The sidebar's see-through background blends with whatever is BEHIND the window,
        // and an off-screen capture has nothing there, so it comes out black. Blending
        // within the window draws the same material from the window itself.
        // Known gap: on macOS 26 the sidebar floats as a glass panel that the system draws
        // outside this process, so its rows (People, Ask, Lists and their counts) are
        // missing from these pictures. Without .fullSizeContentView they appear, but the
        // toolbar does not and the top of every screen fades, which is worse for checking.
        drawSeeThroughWithinWindow(frameView)
        frameView.layoutSubtreeIfNeeded()
        guard let rep = frameView.bitmapImageRepForCachingDisplay(in: frameView.bounds) else { return false }
        frameView.cacheDisplay(in: frameView.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return false }
        do {
            try png.write(to: file)
            return true
        } catch {
            return false
        }
    }

    private static func drawSeeThroughWithinWindow(_ view: NSView) {
        if let effect = view as? NSVisualEffectView, effect.blendingMode == .behindWindow {
            effect.blendingMode = .withinWindow
            effect.state = .active
        }
        for sub in view.subviews { drawSeeThroughWithinWindow(sub) }
    }
}

/// A window that stays where it is put. AppKit pulls a titled window back onto a screen
/// whenever it is shown or resized (`constrainFrameRect`), which would put a picture's
/// window, with whatever data it shows, in front of whoever is at the Mac. This one never moves.
private final class OffscreenWindow: NSWindow {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}
