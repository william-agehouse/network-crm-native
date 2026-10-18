import AppKit
import SwiftUI

// Entry point. The two checking modes are decided here, before SwiftUI exists: by the time
// an App's init runs, SwiftUI has already set up the application and would open the main
// window as soon as the run loop turns, which is exactly what --snapshot and --selftest
// must never do (no window, no Dock icon, and never a connection to the real engine).
//
//   --snapshot <dir>     draw every screen offscreen, light and dark, as PNGs (Snapshot.swift)
//   --selftest           decode every read route of NC_NATIVE_PORT's (or NC_NATIVE_SOCKET's)
//                        engine (SelfTest.swift)
//   --selftest-writes    exercise every write route; refuses unless NC_NATIVE_PORT is 8768, or
//                        NC_NATIVE_SOCKET is an empty engine on scratch data
//   --engine-check       the real start-up path, headless, on a throwaway folder: start the
//                        app's own engine on its private connection (automatic imports off),
//                        check it opened no network port, stopped when the app vanished, and
//                        left nothing running after a quit (NC-330)
//
// Any other launch is the app itself.

@main
enum Launcher {
    @MainActor
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot") {
            let dir = i + 1 < args.count ? args[i + 1] : ""
            runHeadless { await Snapshot.run(directory: dir) }
        }
        if args.contains("--selftest-writes") {
            runHeadless { await SelfTest.run(writes: true) }
        }
        if args.contains("--selftest") {
            runHeadless { await SelfTest.run(writes: false) }
        }
        if args.contains("--engine-check") {
            runHeadless { await engineCheck() }
        }
        NetworkCRMNativeApp.main()
    }

    /// NC-330. The path the app takes when it is opened from Finder, without a window and
    /// on a throwaway folder, never the real data: its own Python and code, its own engine
    /// on the private connection, no network port, and nothing left running afterwards,
    /// whether the app quits or simply vanishes. Prints facts only.
    @MainActor
    private static func engineCheck() async -> Int32 {
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appending(path: "nc-engine-check-\(getpid())")
        defer { try? fm.removeItem(at: scratch) }
        let real = EngineLaunch.forThisApp()
        var launch = EngineLaunch(
            support: scratch.appending(path: "support"), logs: scratch.appending(path: "logs"),
            resources: real.resources, codeFolder: real.codeFolder, python: real.python,
            setsUpOptionalParts: false)
        launch.extraEnvironment = ["NC_AUTOSYNC": "0"]
        print("code: \(real.codeFolder?.path ?? "none")")
        print("python: \(real.python?.path ?? "none")")
        let engine = EngineController(address: .socket(launch.socketPath), launch: launch)
        var failures = 0
        func fail(_ what: String) { failures += 1; print("FAIL: \(what)") }

        func start(_ label: String) async -> Int32? {
            let started = Date()
            await engine.connect()
            let seconds = String(format: "%.1f", Date().timeIntervalSince(started))
            guard case .ready(let s) = engine.state, let pid = engine.engineProcessID else {
                if case .failed(let f) = engine.state { print("FAILED after \(seconds) s: \(f.message) \(f.advice)") }
                let log = (try? String(contentsOf: launch.logFile, encoding: .utf8)) ?? ""
                print(log.suffix(1500))
                return nil
            }
            print("\(label): READY after \(seconds) s on the private connection; people: \(s.peopleCount)")
            return pid
        }
        func gone(_ pid: Int32, within seconds: Double) async -> Bool {
            let deadline = Date().addingTimeInterval(seconds)
            while Date() < deadline {
                if kill(pid, 0) != 0 { return true }
                try? await Task.sleep(for: .milliseconds(100))
            }
            return kill(pid, 0) != 0
        }

        // 1. Start, look at what it opened, then let it see this app vanish.
        guard let first = await start("first start") else { return 1 }
        let (_, listening) = await EngineLaunch.run(
            URL(fileURLWithPath: "/usr/sbin/lsof"), ["-nP", "-a", "-p", String(first), "-iTCP", "-sTCP:LISTEN"],
            in: scratch, log: nil)
        if listening.isEmpty { print("NO NETWORK PORT: the engine listens on none") } else { fail("the engine listens on \(listening)") }
        let fileMode = (try? fm.attributesOfItem(atPath: launch.socketPath)[.posixPermissions] as? Int) ?? -1
        let dirMode = (try? fm.attributesOfItem(atPath: (launch.socketPath as NSString).deletingLastPathComponent)[.posixPermissions] as? Int) ?? -1
        if fileMode == 0o600 && dirMode == 0o700 {
            print("PRIVATE FILE: the connection opens for this account only (600 in a 700 folder)")
        } else {
            fail("the connection's file is \(String(fileMode, radix: 8)) in a \(String(dirMode, radix: 8)) folder")
        }
        engine.dropLifelineForCheck()
        if await gone(first, within: 5) {
            print("VANISHED: with this app gone, its engine stopped by itself")
        } else {
            fail("the engine kept running after its app was gone")
            kill(first, SIGKILL)
        }
        if fm.fileExists(atPath: launch.socketPath) { fail("it left its file behind") }

        // 2. Start again, then quit the way Cmd-Q does.
        await engine.reconnect()
        guard case .ready = engine.state, let second = engine.engineProcessID else {
            fail("the second start did not answer")
            return 1
        }
        print("second start: READY")
        engine.stopEngineIfOwned()
        if await gone(second, within: 1) { print("QUIT: nothing left running") } else { fail("still running after quit") }
        if fm.fileExists(atPath: launch.socketPath) { fail("its file is still there after quit") }
        print(failures == 0 ? "ENGINE CHECK PASSED" : "ENGINE CHECK FAILED: \(failures)")
        return failures == 0 ? 0 : 1
    }

    /// Runs `body` with a live AppKit run loop (offscreen windows need one) but no Dock
    /// icon and no menu bar, then exits with its code.
    @MainActor
    private static func runHeadless(_ body: @escaping @MainActor () async -> Int32) -> Never {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in
            let code = await body()
            fflush(stdout)
            exit(code)
        }
        app.run()
        exit(0)
    }
}

struct NetworkCRMNativeApp: App {
    @State private var app = AppModel(engine: .fromEnvironment())
    /// "system", "light" or "dark": set on the Settings page and from ⌘K.
    @AppStorage("appearance") private var appearance = "system"

    private var engineAnswering: Bool { app.engine.isReady && app.engine.trouble == nil }

    var body: some Scene {
        // One window is the app: a Window, not a WindowGroup, so there is never a second
        // copy of the screens, and the Window menu brings it back after ⌘W.
        Window("Network CRM", id: "main") {
            RootView()
                .environment(app)
                .frame(minWidth: 900, minHeight: 600)
                .preferredColorScheme(appearance == "dark" ? .dark : appearance == "light" ? .light : nil)
        }
        .defaultSize(width: 1200, height: 780)
        .commands {
            // One window is the app, so File > New Window goes; that frees ⌘N for the
            // screens' own New Chat, New List and Add Person.
            // The items that need the engine are off while it is not there: each would open
            // a sheet or a save panel and then fail with nowhere to say so.
            CommandGroup(replacing: .newItem) {
                Group {
                    Button("Add Person…") {
                        app.section = .people
                        app.showAddPerson = true
                    }
                    Button("Paste a List of People…") { app.setupFlow = .paste }
                }
                .disabled(!engineAnswering)
                Divider()
                Button("Import and Sync…") { app.section = .data }
                Group {
                    Button("Find Duplicates…") { CommandActions.findDuplicates(app) }
                    Button("Merge Two People…") { app.mergeRequest = MergeRequest() }
                    Button("Export a Backup…") { CommandActions.exportBackup(app) }
                        .keyboardShortcut("e", modifiers: [.command, .shift])
                }
                .disabled(!engineAnswering)
            }
            // Settings is a page in the window, as in the web page, so ⌘, goes there.
            CommandGroup(replacing: .appSettings) {
                Button("Settings…") { app.section = .settings }
                    .keyboardShortcut(",")
            }
            CommandGroup(replacing: .help) {
                Button("Set Up Network CRM…") { app.showOnboarding = true }
            }
            // Replaces the text-editing group so ⌘F means "search people", as it does in
            // the web page, instead of the system's find bar.
            CommandGroup(replacing: .textEditing) {
                Button("Search People") { app.focusSearch() }
                    .keyboardShortcut("f")
            }
            CommandMenu("Go") {
                Button("Search and Commands…") { app.showCommandBar = true }
                    .keyboardShortcut("k")
                Divider()
                ForEach(Array(AppModel.Section.allCases.enumerated()), id: \.element) { index, section in
                    Button(section.title) { app.section = section }
                        .keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: .command)
                }
            }
        }
    }
}
