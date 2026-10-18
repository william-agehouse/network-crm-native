import AppKit
import Observation
import SwiftUI

// Remove everything this app keeps on this Mac (NC-314), at the very bottom of the Settings
// page, as on the web page (static/app/settings.js RemoveEverything). Since NC-330 this window
// is the only app, so without it the README, docs/UNINSTALL.md and the disk image's Read Me
// would send people to a button that does not exist.
//
//   GET  /api/remove/plan              what would go, whether this copy may do it, the word
//                                      to type, and how long the helper's uninstaller may take
//   POST /api/remove/everything        {confirm: word}. Turns the copying helper off, then
//                                      deletes the app's folder and its logs, then the engine
//                                      stops itself. NEVER called by the self-tests.
//
// Built to be hard to press by accident: the first press only shows what goes, and the button
// that does it stays off until the word the engine asks for is typed. The engine stops a
// moment after it answers, so the answer is the last thing this page shows, and nothing on
// that last screen asks the engine anything.

struct RemovePart: Codable, Sendable, Hashable, HasDefault {
    static var defaultValue: RemovePart { RemovePart() }
    @Def var name: String
    @Def var path: String
}

struct RemoveWaits: Codable, Sendable, Hashable, HasDefault {
    static var defaultValue: RemoveWaits { RemoveWaits() }
    /// Seconds the helper's uninstaller may take (api_fda.INSTALL_TIMEOUT).
    @Def var uninstall: Int
}

struct RemovePlan: Codable, Sendable, Hashable {
    @Def var allowed: Bool
    @Def var whyNot: String
    @Def var word: String
    @Def var removes: [RemovePart]
    @Def var byHand: [String]
    @Def var waits: RemoveWaits
    enum CodingKeys: String, CodingKey {
        case allowed, word, removes, waits
        case whyNot = "why_not"
        case byHand = "by_hand"
    }
}

struct RemoveHelperRun: Codable, Sendable, Hashable, HasDefault {
    static var defaultValue: RemoveHelperRun { RemoveHelperRun() }
    @Def var ok: Bool
    @Def var ran: Bool
    @Def var output: String
}

struct RemoveResult: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var says: String
    @Def var left: [RemovePart]
    @Def var helper: RemoveHelperRun
    @Def var byHand: [String]
    @Def var stopping: Bool
    enum CodingKeys: String, CodingKey {
        case ok, says, left, helper, stopping
        case byHand = "by_hand"
    }
}

@MainActor
@Observable
final class RemoveEverythingModel {
    private(set) var plan: RemovePlan?
    private(set) var result: RemoveResult?
    var armed = false
    var word = ""
    private(set) var busy = false
    private(set) var note = ""

    /// The word typed matches the one the engine asks for (case and spaces aside, as on the web).
    var wordMatches: Bool {
        guard let plan, !plan.word.isEmpty else { return false }
        return word.trimmingCharacters(in: .whitespaces).lowercased() == plan.word
    }

    func load(_ client: EngineClient?) async {
        guard let client else { return }
        do {
            plan = try await client.get("/api/remove/plan", as: RemovePlan.self)
        } catch is CancellationError {
        } catch {
            var p = RemovePlan()
            p.whyNot = "Could not ask what this would remove just now."
            plan = p
        }
    }

    func disarm() {
        armed = false
        word = ""
        note = ""
    }

    func typed() { note = "" }

    func run(_ client: EngineClient?) async {
        guard let client, let plan, !busy else { return }
        guard wordMatches else {
            note = "Type \(plan.word) in the box first."
            return
        }
        busy = true
        note = ""
        defer { busy = false }
        do {
            let wait = TimeInterval(max(plan.waits.uninstall, 0) + 30)
            result = try await client.post(
                "/api/remove/everything",
                body: ["confirm": .string(word.trimmingCharacters(in: .whitespaces))] as [String: JSONValue],
                as: RemoveResult.self, timeout: wait)
        } catch is CancellationError {
        } catch {
            note = EngineError.sentence(for: error)
        }
    }
}

// from static/app/settings.js RemoveEverything
struct SettingsRemoveEverything: View {
    @AppEnvironment private var app
    @State private var model = RemoveEverythingModel()

    var body: some View {
        LedgerStatusRow(label: "Remove everything") {
            content
        }
        .task { await model.load(app.client) }
    }

    @ViewBuilder private var content: some View {
        if let done = model.result {
            finished(done)
        } else if let plan = model.plan {
            if !plan.allowed {
                VStack(alignment: .leading, spacing: 6) {
                    Button("Remove Everything", role: .destructive) {}.disabled(true)
                    quiet(plan.whyNot)
                }
            } else if !model.armed {
                VStack(alignment: .leading, spacing: 6) {
                    Button("Remove Everything…", role: .destructive) { model.armed = true }
                    quiet("Deletes every person, message and setting this app keeps on this Mac, and turns automatic copying off. It cannot be undone.")
                }
            } else {
                armed(plan)
            }
        }
    }

    private func armed(_ plan: RemovePlan) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("\(Text("This deletes everything and cannot be undone.").bold()) Export a backup on the Data page first if you may want any of it back. What goes:")
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 3) {
                ForEach(plan.removes, id: \.path) { part in
                    Text(verbatim: "•  \(part.name)").help(part.path)
                }
            }
            .foregroundStyle(Palette.ink)
            quiet("Afterwards you drag the app to the Trash and remove one entry in System Settings yourself. The last screen says how.")
            HStack(spacing: 10) {
                Text(verbatim: "Type \(plan.word) to confirm").foregroundStyle(Palette.muted)
                TextField("", text: $model.word)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .frame(width: 160)
                    .disabled(model.busy)
                    .onChange(of: model.word) { model.typed() }
                    .onSubmit { Task { await model.run(app.client) } }
                    .onExitCommand { model.disarm() }
                Button(model.busy ? "Removing…" : "Remove Everything for Good", role: .destructive) {
                    Task { await model.run(app.client) }
                }
                .disabled(model.busy || !model.wordMatches)
                Button("Keep It All") { model.disarm() }
                    .disabled(model.busy)
            }
            if !model.note.isEmpty {
                Text(verbatim: model.note).foregroundStyle(Palette.muted)
            }
        }
        .font(.system(size: Metrics.fsUI))
    }

    private func finished(_ done: RemoveResult) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(verbatim: done.says).bold().foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !done.left.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(done.left, id: \.path) { part in
                        Text(verbatim: "•  \(part.name): \(part.path)").textSelection(.enabled)
                    }
                }
            }
            if !done.ok && !done.helper.output.isEmpty {
                Text(verbatim: done.helper.output)
                    .font(.system(size: Metrics.fsUI - 1, design: .monospaced))
                    .foregroundStyle(Palette.muted)
                    .textSelection(.enabled)
            }
            if !done.byHand.isEmpty {
                Text("Two steps are left, and macOS lets only you do them:").foregroundStyle(Palette.ink)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(done.byHand.enumerated()), id: \.offset) { i, step in
                        Text(verbatim: "\(i + 1). \(step)").fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            if done.stopping {
                quiet("The app's engine has stopped, so nothing more can be shown or saved. Quit the app now.")
                Button("Quit Network CRM") { NSApp.terminate(nil) }
            }
        }
        .font(.system(size: Metrics.fsUI))
    }

    private func quiet(_ text: String) -> some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}
