import AppKit
import CryptoKit
import Foundation
import Observation

// NC-330. The engine (the Python code in crm/) is this app's own child. The app starts it
// when it opens, reaches it through a private socket file (PrivateConnection.swift), and
// stops it when it quits. Nothing else on the Mac can reach it: there is no network
// address at all, the file and its folder open for this account only, and the engine stops
// by itself when the app goes away without saying so (a crash or a force quit closes the
// pipe on its standard input). Until 2026-10-17 a shell script started the engine on
// 127.0.0.1:8765, where any browser on the Mac could also open it, and a separate "Quit
// Network CRM" app stopped it.
//
// Development keeps a way in: NC_NATIVE_PORT joins an engine someone started on 127.0.0.1
// (the self-tests' scratch engine on 8768), and NC_NATIVE_SOCKET one started with
// --socket. Neither ever starts or stops an engine. Finder passes no environment, so the
// app opened from Finder always starts its own.

@MainActor
@Observable
final class EngineController {
    enum State: Equatable {
        /// About to start, or (development) asking whether the engine answers.
        case connecting
        /// The first start of a new version: making the private Python environment the
        /// optional parts run in (about a minute, and it needs the internet).
        case settingUp
        /// The engine was started and is not answering yet.
        case starting
        case ready(EngineStatus)
        case failed(Failure)
    }

    /// What went wrong, and what the person can do about it, in plain words.
    struct Failure: Equatable, Sendable {
        let message: String
        let advice: String
    }

    private(set) var state: State = .connecting
    /// True while this app's own engine runs (so quitting stops it).
    private(set) var startedByThisApp = false
    /// The engine shown has missed status checks in a row. The screens stay as they are
    /// (with whatever was being typed), a banner says so, and the next check that answers
    /// clears it, as the web page's red status dot does (main.js:636-643).
    private(set) var trouble: Failure?

    let address: EngineAddress
    /// What starting the engine needs, or nil in development (never starts one).
    let launch: EngineLaunch?
    let client: EngineClient

    /// The port in development, 0 for the app's private connection.
    var port: Int { address.port }
    /// Development: NC_NATIVE_PORT or NC_NATIVE_SOCKET was set, so this app never starts
    /// or stops an engine itself.
    var isDevelopmentPort: Bool { launch == nil }

    /// How long a freshly started engine may take to answer. Its first start on a new
    /// data folder creates the databases, which is why this is not shorter.
    static let startupWait: TimeInterval = 30
    /// How long a quitting engine may take before it and the programs it started are
    /// stopped outright.
    static let stopWait: TimeInterval = 3
    /// Status checks missed in a row before the banner says the engine stopped answering:
    /// one slow answer (a big import holding the database) is not a lost engine.
    static let missesBeforeTrouble = 2

    @ObservationIgnored private var process: Process?
    /// The write end of the engine's standard input. Never written to; closing it (or this
    /// app ending, however it ends) tells the engine to stop.
    @ObservationIgnored private var lifeline: FileHandle?
    @ObservationIgnored private var missedChecks = 0
    @ObservationIgnored private var connectTask: Task<Void, Never>?
    @ObservationIgnored private var quitObserver: NSObjectProtocol?

    var status: EngineStatus? {
        if case .ready(let s) = state { return s }
        return nil
    }
    var isReady: Bool { status != nil }

    /// Development: an engine already listening on 127.0.0.1:port.
    convenience init(port: Int, isDevelopmentPort: Bool = true) {
        self.init(address: .port(port), launch: nil)
    }

    init(address: EngineAddress, launch: EngineLaunch?) {
        self.address = address
        self.launch = launch
        self.client = EngineClient(address: address)
        quitObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.stopEngineIfOwned() }
        }
    }

    /// The app: its own engine on its private connection. Development: NC_NATIVE_PORT or
    /// NC_NATIVE_SOCKET, which only a shell sets.
    static func fromEnvironment() -> EngineController {
        let env = ProcessInfo.processInfo.environment
        if let raw = env["NC_NATIVE_PORT"], let p = Int(raw), p > 0, p < 65536 {
            return EngineController(address: .port(p), launch: nil)
        }
        if let path = env["NC_NATIVE_SOCKET"], !path.isEmpty {
            return EngineController(address: .socket(path), launch: nil)
        }
        let launch = EngineLaunch.forThisApp()
        return EngineController(address: .socket(launch.socketPath), launch: launch)
    }

    // MARK: Connecting

    /// Connect, starting the engine if this is the app. Safe to call repeatedly: a second
    /// call while one is under way waits for that one.
    func connect() async {
        if let running = connectTask { return await running.value }
        let task = Task { await self.runConnect() }
        connectTask = task
        await task.value
        connectTask = nil
    }

    /// The Retry / Reconnect button.
    func reconnect() async {
        state = .connecting
        await connect()
    }

    /// Fresh `/api/status`, updating the ready state. A missed answer leaves the screens up
    /// and, after `missesBeforeTrouble` in a row, sets `trouble`; the next answer clears it.
    @discardableResult
    func refreshStatus() async -> EngineStatus? {
        do {
            let s = try await client.get("/api/status", as: EngineStatus.self, timeout: 5)
            missedChecks = 0
            trouble = nil
            state = .ready(s)
            return s
        } catch {
            guard isReady else { return nil }
            if launch != nil, let p = process, !p.isRunning {
                // This app's engine has stopped: no point waiting for a second miss.
                process = nil
                startedByThisApp = false
                closeLifeline()
                trouble = Failure(
                    message: "The engine stopped.",
                    advice: "What is on screen stays, but nothing can be saved. Press Retry to start it again. "
                        + "What it said is in ~/Library/Logs/NetworkCRM/server.log.")
                return nil
            }
            missedChecks += 1
            if missedChecks >= Self.missesBeforeTrouble {
                trouble = Failure(
                    message: "The engine stopped answering.",
                    advice: "What is on screen stays, but nothing can be saved until it answers again. This app checks every few seconds.")
            }
            return nil
        }
    }

    private func runConnect() async {
        trouble = nil
        missedChecks = 0
        guard let launch else {
            state = .connecting
            await joinDevelopmentEngine()
            return
        }
        // This app's engine has not answered yet (a slow first start, then Retry): keep
        // waiting for it rather than starting a second one beside it.
        if let p = process, p.isRunning {
            state = .starting
            await waitForStart(of: p, launch: launch)
            return
        }
        await startEngine(launch)
    }

    private func joinDevelopmentEngine() async {
        if let s = try? await client.get("/api/status", as: EngineStatus.self, timeout: 5) {
            state = .ready(s)
            return
        }
        switch address {
        case .port(let p):
            state = .failed(Failure(
                message: "Nothing answers on port \(p).",
                advice: "NC_NATIVE_PORT is set, so this app does not start an engine itself. Start one on that port, then press Retry."))
        case .socket(let path):
            state = .failed(Failure(
                message: "Nothing answers at \(path).",
                advice: "NC_NATIVE_SOCKET is set, so this app does not start an engine itself. Start one with --socket at that path, then press Retry."))
        }
    }

    // MARK: Starting the engine

    private func startEngine(_ launch: EngineLaunch) async {
        state = .connecting
        guard let code = launch.codeFolder else {
            state = .failed(Failure(
                message: "This copy of Network CRM has no engine inside it.",
                advice: "Download Network CRM again and replace this copy. Your people are kept; they are not inside the app."))
            return
        }
        guard let basePython = launch.python else {
            state = .failed(Failure(
                message: "This copy of Network CRM has no Python inside it, so its engine cannot start.",
                advice: "Download Network CRM again and replace this copy. Your people are kept; they are not inside the app."))
            return
        }
        do {
            try launch.makeFolders()
        } catch {
            state = .failed(Failure(
                message: "Network CRM could not make its folder in your Library.",
                advice: "Check there is free space on this Mac, then press Retry. The folder is ~/Library/Application Support/NetworkCRM."))
            return
        }
        // Another copy of the app already runs an engine on this data. Its engine is not
        // this app's to use or stop, and a second one must never open the same databases.
        if PrivateConnection.answers(launch.socketPath) {
            state = .failed(Failure(
                message: "Network CRM is already open on this Mac, in another copy of the app.",
                advice: "Quit the other copy, then press Retry."))
            return
        }
        await Task.detached { launch.stopEngineFromBeforeNC330() }.value

        var python = basePython
        if launch.setsUpOptionalParts {
            if let ready = await launch.readyPython(base: basePython, code: code) {
                python = ready
            } else {
                state = .settingUp
                python = await launch.setUpOptionalParts(base: basePython, code: code) ?? basePython
            }
        }

        state = .starting
        let log = launch.openLog(note: "Network CRM starting its engine")
        let p = Process()
        p.executableURL = python
        // Relative script path from the code folder, as before, so anything that recognises
        // the engine by "crm/app.py" on its command line still does.
        p.arguments = ["crm/app.py", "--socket", launch.socketPath, "--watch-stdin"]
        p.currentDirectoryURL = code
        p.environment = launch.environment(code: code)
        let pipe = Pipe()
        p.standardInput = pipe
        if let log {
            p.standardOutput = log
            p.standardError = log
        } else {
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
        }
        do {
            try p.run()
        } catch {
            try? log?.close()
            state = .failed(Failure(
                message: "The engine could not be started.",
                advice: "Press Retry. What went wrong is in ~/Library/Logs/NetworkCRM/server.log."))
            return
        }
        try? log?.close()
        // Only this app holds the write end: the engine sees its input close the moment
        // this app ends, however it ends.
        try? pipe.fileHandleForReading.close()
        lifeline = pipe.fileHandleForWriting
        process = p
        startedByThisApp = true
        await waitForStart(of: p, launch: launch)
    }

    /// Wait for the engine this app started to answer, or to stop.
    private func waitForStart(of p: Process, launch: EngineLaunch) async {
        let deadline = Date().addingTimeInterval(Self.startupWait)
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: launch.socketPath),
               let s = try? await client.get("/api/status", as: EngineStatus.self, timeout: 2)
            {
                state = .ready(s)
                return
            }
            if !p.isRunning {
                process = nil
                startedByThisApp = false
                closeLifeline()
                state = .failed(Failure(
                    message: "The engine started and then stopped.",
                    advice: "Press Retry. If it stops again, what it said is in ~/Library/Logs/NetworkCRM/server.log."))
                return
            }
            try? await Task.sleep(for: .milliseconds(150))
        }
        state = .failed(Failure(
            message: "The engine did not answer within \(Int(Self.startupWait)) seconds.",
            advice: "Press Retry; a first start can be slow. What it said is in ~/Library/Logs/NetworkCRM/server.log."))
    }

    // MARK: Stopping

    /// Stop this app's engine and every program it started: close its input and ask it to
    /// stop, then, after `stopWait`, stop the whole group outright. Never touches an engine
    /// this app did not start.
    func stopEngineIfOwned() {
        guard startedByThisApp, let p = process else { return }
        closeLifeline()
        if p.isRunning {
            p.terminate()
            let deadline = Date().addingTimeInterval(Self.stopWait)
            while p.isRunning && Date() < deadline { usleep(30_000) }
            if p.isRunning {
                // The engine leads its own process group (crm/app.py calls setpgrp), so this
                // reaches the import programs it started as well.
                killpg(p.processIdentifier, SIGKILL)
                kill(p.processIdentifier, SIGKILL)
            }
        }
        process = nil
        startedByThisApp = false
    }

    /// --engine-check only: what a crash or a force quit of this app does to the engine,
    /// without the crash. Closes its input and nothing else.
    func dropLifelineForCheck() {
        closeLifeline()
    }

    /// The engine process this app started, for --engine-check.
    var engineProcessID: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }

    private func closeLifeline() {
        try? lifeline?.close()
        lifeline = nil
    }

    // MARK: Stamps the engine reports back

    /// The fingerprint the engine reports on /api/status (`instance`): the first 16 hex
    /// characters of the SHA-256 of "<code folder>\n<data folder>\n", as the launcher before
    /// NC-330 computed it, so an older engine's answer still compares.
    nonisolated static func fingerprint(appHome: String, dataFolder: String) -> String {
        let digest = SHA256.hash(data: Data("\(appHome)\n\(dataFolder)\n".utf8))
        return String(digest.map { String(format: "%02x", $0) }.joined().prefix(16))
    }

    /// The build stamp appify.sh writes into the bundle (NC-203), kept to letters, digits
    /// and "-".
    nonisolated static func buildID(resources: URL) -> String {
        let raw = (try? String(contentsOf: resources.appending(path: "build_id.txt"), encoding: .utf8)) ?? ""
        return String(raw.unicodeScalars.filter {
            ("A"..."Z").contains($0) || ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "-"
        }.map(Character.init))
    }
}

/// Everything starting the app's engine needs: where its code, Python and data are. Built
/// once from the app bundle; `--engine-check` builds one on a scratch folder instead.
struct EngineLaunch: Sendable {
    /// ~/Library/Application Support/NetworkCRM: the data, the secrets, the Python
    /// environment and the private connection's folder.
    let support: URL
    /// ~/Library/Logs/NetworkCRM.
    let logs: URL
    /// The bundle's Contents/Resources.
    let resources: URL
    /// The engine's code: Resources/app inside a built app, or the checkout a developer
    /// build names in Resources/repo_path.txt.
    let codeFolder: URL?
    /// The Python the engine runs on before any optional package: the one inside the app,
    /// or a developer build's checkout's own .venv.
    let python: URL?
    /// Make the private environment for the optional parts first (the app's own Python).
    /// Off for a developer build, whose checkout venv has them, and for --engine-check.
    let setsUpOptionalParts: Bool
    /// Added last to the engine's environment (--engine-check turns automatic imports off).
    var extraEnvironment: [String: String] = [:]

    var dataFolder: URL { support.appending(path: "data") }
    var secretsFolder: URL { support.appending(path: "secrets") }
    var venv: URL { support.appending(path: "venv") }
    var logFile: URL { logs.appending(path: "server.log") }

    /// The private connection's file. A socket file's whole path must fit in 103 bytes; a
    /// very long account name would overflow the Library one, and then the account's own
    /// temporary folder, which also opens for this account only, is used instead.
    var socketPath: String {
        let preferred = support.appending(path: "run/engine.sock").path
        if preferred.utf8.count <= PrivateConnection.longestPath { return preferred }
        return URL(fileURLWithPath: NSTemporaryDirectory()).appending(path: "NetworkCRM/engine.sock").path
    }

    static func forThisApp() -> EngineLaunch {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        let resources = Bundle.main.resourceURL ?? Bundle.main.bundleURL.appending(path: "Contents/Resources")
        let embedded = resources.appending(path: "app")
        var code: URL?
        var python: URL?
        var setsUp = false
        if fm.fileExists(atPath: embedded.appending(path: "crm/app.py").path) {
            code = embedded
            let bundled = resources.appending(path: "python/bin/python3")
            if fm.isExecutableFile(atPath: bundled.path) {
                python = bundled
                setsUp = true
            }
        } else if let raw = try? String(contentsOf: resources.appending(path: "repo_path.txt"), encoding: .utf8) {
            let repo = URL(fileURLWithPath: raw.trimmingCharacters(in: .whitespacesAndNewlines))
            if fm.fileExists(atPath: repo.appending(path: "crm/app.py").path) {
                code = repo
                python = [repo.appending(path: ".venv/bin/python3"),
                          repo.deletingLastPathComponent().appending(path: ".venv/bin/python3")]
                    .first { fm.isExecutableFile(atPath: $0.path) }
            }
        }
        return EngineLaunch(
            support: home.appending(path: "Library/Application Support/NetworkCRM"),
            logs: home.appending(path: "Library/Logs/NetworkCRM"),
            resources: resources, codeFolder: code, python: python, setsUpOptionalParts: setsUp)
    }

    func makeFolders() throws {
        let fm = FileManager.default
        // NC-321: everything the app keeps opens for this account only.
        for dir in [support, dataFolder, secretsFolder, logs] {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true,
                                   attributes: [.posixPermissions: 0o700])
        }
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: secretsFolder.path)
    }

    /// The engine's environment: what this app was given, less anything that would point
    /// the engine at other code, other data or another Python, plus its own folders.
    func environment(code: URL) -> [String: String] {
        var env = ProcessInfo.processInfo.environment.filter { key, _ in
            !(key.hasPrefix("PYTHON") || key.hasPrefix("NC_") || key.hasPrefix("CRM_")
              || key == "VIRTUAL_ENV" || key == "APP_PORT" || key == "APP_HOST" || key == "NETWORK_CRM_HOME")
        }
        env["HOME"] = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env["NC_NO_BROWSER"] = "1"
        env["CRM_DATA_DIR"] = dataFolder.path
        env["CRM_SECRETS_DIR"] = secretsFolder.path
        // NC-310: no compiled caches written beside code inside the app.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["NC_INSTANCE"] = EngineController.fingerprint(appHome: code.path, dataFolder: dataFolder.path)
        env["NC_BUILD"] = EngineController.buildID(resources: resources)
        // NC-315: where new versions come from, for a copy built to be handed out.
        if let raw = try? String(contentsOf: resources.appending(path: "update_repo.txt"), encoding: .utf8) {
            let repo = String(raw.unicodeScalars.filter {
                CharacterSet.alphanumerics.contains($0) || "._/-".unicodeScalars.contains($0)
            }.map(Character.init))
            if !repo.isEmpty { env["NC_UPDATE_REPO"] = repo }
        }
        return env.merging(extraEnvironment) { _, extra in extra }
    }

    /// Appends a dated line to the engine's log and hands back the file to write to.
    func openLog(note: String) -> FileHandle? {
        let fm = FileManager.default
        try? fm.createDirectory(at: logs, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        if !fm.fileExists(atPath: logFile.path) {
            fm.createFile(atPath: logFile.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let handle = try? FileHandle(forWritingTo: logFile) else { return nil }
        _ = try? handle.seekToEnd()
        handle.write(Data("\n--- \(Date()) \(note)\n".utf8))
        return handle
    }

    // MARK: The optional parts' Python environment

    /// The environment's Python when it is ready for this version's package list, or nil
    /// when setting up is needed. Quick: it only looks (crm/python_env.py check).
    func readyPython(base: URL, code: URL) async -> URL? {
        let (status, out) = await Self.run(base, ["-I", "crm/python_env.py", "check",
                                                  "--venv", venv.path, "--app-home", code.path],
                                           in: code, log: nil)
        guard status == 0, let line = out.last, FileManager.default.isExecutableFile(atPath: line) else { return nil }
        return URL(fileURLWithPath: line)
    }

    /// Makes or repairs the environment and installs the packages. Returns the Python the
    /// engine should run on; a failed install still returns one, and the Settings page says
    /// what did not install.
    func setUpOptionalParts(base: URL, code: URL) async -> URL? {
        let log = openLog(note: "Network CRM setting up its optional parts")
        defer { try? log?.close() }
        let (_, out) = await Self.run(base, ["-I", "crm/python_env.py", "prepare",
                                             "--venv", venv.path, "--app-home", code.path,
                                             "--data", dataFolder.path, "--log", logFile.path],
                                      in: code, log: log)
        guard let line = out.last, FileManager.default.isExecutableFile(atPath: line) else { return nil }
        return URL(fileURLWithPath: line)
    }

    /// Runs a program to the end without holding up the window; its output's lines back.
    nonisolated static func run(_ exe: URL, _ args: [String], in dir: URL, log: FileHandle?) async -> (Int32, [String]) {
        await withCheckedContinuation { (done: CheckedContinuation<(Int32, [String]), Never>) in
            let p = Process()
            p.executableURL = exe
            p.arguments = args
            p.currentDirectoryURL = dir
            var env = ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("PYTHON") }
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONNOUSERSITE"] = "1"
            p.environment = env
            let out = Pipe()
            p.standardOutput = out
            p.standardError = log ?? FileHandle.nullDevice
            p.standardInput = FileHandle.nullDevice
            do {
                try p.run()
            } catch {
                done.resume(returning: (-1, []))
                return
            }
            DispatchQueue.global(qos: .userInitiated).async {
                let data = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                let lines = String(decoding: data, as: UTF8.self)
                    .split(whereSeparator: \.isNewline).map(String.init)
                done.resume(returning: (p.terminationStatus, lines))
            }
        }
    }

    // MARK: The engine an older version started

    /// Versions before NC-330 started the engine on 127.0.0.1:8765 and wrote its process
    /// number to networkcrm.pid. One left running would keep that address open, and would
    /// write to the same databases as this app's own engine. Stopped once, here; only a
    /// process whose command line proves it is Network CRM's engine.
    func stopEngineFromBeforeNC330() {
        let pidFile = support.appending(path: "networkcrm.pid")
        guard let raw = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = Int32(raw.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1 else { return }
        defer { try? FileManager.default.removeItem(at: pidFile) }
        guard kill(pid, 0) == 0, Self.commandLine(of: pid).contains("crm/app.py") else { return }
        kill(pid, SIGTERM)
        let deadline = Date().addingTimeInterval(3)
        while kill(pid, 0) == 0 && Date() < deadline { usleep(50_000) }
    }

    /// A process's full command line, from the system itself (no program run).
    nonisolated static func commandLine(of pid: Int32) -> String {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var size = 0
        guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return "" }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctl(&mib, 3, &buffer, &size, nil, 0) == 0 else { return "" }
        // argc, then the executable path, padding, then the arguments, all 0-separated.
        let argc = buffer.withUnsafeBytes { $0.load(as: Int32.self) }
        var parts: [String] = []
        var i = MemoryLayout<Int32>.size
        while i < size && buffer[i] != 0 { i += 1 }          // executable path
        while i < size && buffer[i] == 0 { i += 1 }          // padding
        while i < size && parts.count < Int(argc) {
            var j = i
            while j < size && buffer[j] != 0 { j += 1 }
            parts.append(String(decoding: buffer[i..<j], as: UTF8.self))
            i = j + 1
        }
        return parts.joined(separator: " ")
    }
}
