import AppKit
import SwiftUI

// The shared parts of the Data and Settings pages, so both are learned once
// (static/app/ledger.js):
//
//   LedgerSection     a titled part of a page: Needs you, the ledger, This install
//   LedgerRowView     one named thing, one line, opened by clicking it — the ONE way to
//                     reveal more on either page
//   LedgerDetailView  what an open row shows: five blocks, always in the same order
//   LedgerNeedView    one thing the app cannot do without you, with the fix
//   LedgerStatusRow   one read-only figure about the whole install
//
// and the leaf controls every row uses: the tone dot, the effort chip, a figure, the
// credential box (Connect, then Replace or Remove: a stored secret is never edited in
// place), a plain setting and an on/off switch. Nothing here decides anything: rows,
// tones and sentences come from the engine; these parts only draw them.

/// What a page offers the controls inside its rows. Both page stores conform.
@MainActor
protocol LedgerHost: AnyObject {
    var app: AppModel? { get }
    /// One line at the foot of the page, for a moment (the web page's toast).
    func flash(_ text: String)
    /// Ask the engine again after anything changed (NC-125: there is no refresh button).
    func changed() async
    /// Open one row of this page and bring it into view.
    func openRow(_ key: String)
}

/// The one write behind every setting box and switch on both pages.
// from static/app/ledger.js:242-244
enum LedgerSettings {
    @MainActor
    static func save(_ client: EngineClient, key: String, value: String) async throws -> SettingsSaveResult {
        try await client.post(
            "/api/settings", body: ["key": .string(key), "value": .string(value)] as [String: JSONValue],
            as: SettingsSaveResult.self)
    }

    /// One sentence for a failure, never a status code or a Python exception. A 502 or
    /// 504 is the engine's own plain sentence about something outside it (a calendar
    /// address that stopped working, a helper that timed out; api_import.py, api_fda.py),
    /// so it is shown as the web page shows it (data.js:99). A 500 may be an exception's
    /// text, so it is not. NC-363: EngineError decides which is which (and keeps
    /// the route and the status for the log), so this only finishes a 502 or 504 into a
    /// sentence with a capital and a full stop.
    static func sentence(_ error: Error) -> String {
        let said = EngineError.sentence(for: error)
        if case .engine(let status, let message)? = error as? EngineError,
            status == 502 || status == 504, !message.isEmpty
        {
            return SetupWords.finished(said)
        }
        return said
    }

    /// Ask the engine to reveal an allowlisted folder or open a page. The app only ever
    /// sends a key; the path lives in importers.OPEN_TARGETS.
    // from static/app/ui.js:29-32
    @MainActor
    static func open(_ target: String, host: LedgerHost) async {
        guard let client = host.app?.client else { return }
        do {
            let r = try await client.post(
                "/api/import/open", body: ["source": .string(target)] as [String: JSONValue], as: DataOpenResult.self)
            // The web page says nothing when macOS refused (ui.js:29-32); a button that
            // does nothing at all reads as broken, so say so.
            if !r.ok { host.flash("macOS did not open it. Try again, or open it yourself in Finder.") }
        } catch is CancellationError {
        } catch {
            host.flash(sentence(error))
        }
    }
}

// MARK: - Icons

enum LedgerIcons {
    /// SF Symbols for each row, standing in for static/app/icons.js glyphs
    /// (data.js:31-32, settings.js:24-27).
    static func symbol(_ key: String) -> String {
        switch key {
        case "helper", "autosync": "arrow.triangle.2.circlepath"
        case "whatsapp": Display.channelSymbol(.whatsapp)
        case "imessage": Display.channelSymbol(.imessage)
        case "contacts": "person.crop.rectangle.stack"
        case "calendar_local", "calendar", "ics_fetch": "calendar"
        case "calendar_google", "calendar_icloud": "link"
        case "linkedin_connections", "linkedin_messages", "linkedin_facts": Display.channelSymbol(.linkedin)
        case "email": Display.channelSymbol(.email)
        case "gmail": "envelope.badge"
        case "google_photos": "photo.on.rectangle"
        case "facts_llm", "ai": "sparkles"
        case "worked_out": "chart.bar"
        case "identity": "person"
        case "goals": "pencil"
        case "intake_web": "globe"
        case "dates": "birthday.cake"
        case "semantic": "magnifyingglass"
        default: "circle.dotted"
        }
    }
}

// MARK: - Dots, chips, figures

/// The state of one row, as a dot: green working, gold falling behind, red stopped,
/// grey paused, ink waiting for you, a ring when off or not in this copy.
// from static/css/view-ledger.css:55-66
struct LedgerToneDot: View {
    let tone: String
    var body: some View {
        Group {
            switch tone {
            case "off", "na":
                Circle().strokeBorder(Palette.lineStrong, lineWidth: 1.5)
            default:
                Circle().fill(color)
            }
        }
        .frame(width: 8, height: 8)
        .help(LedgerWords.tone(tone))
        .accessibilityLabel(LedgerWords.tone(tone))
    }

    private var color: Color {
        switch tone {
        case "ok": Palette.green
        case "behind": Palette.gold
        case "blocked": Palette.red
        case "ready": Palette.ink
        default: Palette.faint
        }
    }
}

/// What switching something on takes from you, in words only (NC-230).
// from static/app/ledger.js:110-113, view-ledger.css:68-72
struct LedgerCostChip: View {
    let cost: LedgerCost
    var body: some View {
        if !cost.label.isEmpty {
            Text(verbatim: cost.label.uppercased())
                .font(.system(size: 10.5, weight: .regular, design: .monospaced))
                .tracking(0.6)
                .foregroundStyle(Palette.muted)
                .lineLimit(1)
                .fixedSize()
        }
    }
}

/// A number that says what it counts; hovering names the people it counts.
struct LedgerFigureText: View {
    let fig: LedgerFigure?
    var words: String = ""
    var whole: Bool = true
    var body: some View {
        if let f = LedgerWords.figure(fig, words: words, whole: whole) {
            Text("\(Text(verbatim: f.lead).fontWeight(.semibold))\(f.tail.isEmpty ? "" : " " + f.tail)")
                .foregroundStyle(Palette.ink)
                .font(.system(size: Metrics.fsUI))
                .help(f.help)
        }
    }
}

/// The small overline used for block titles and column heads.
struct LedgerOverline: View {
    let text: String
    var body: some View {
        Text(verbatim: text.uppercased())
            .font(.system(size: 10.5, weight: .medium, design: .monospaced))
            .tracking(0.6)
            .foregroundStyle(Palette.muted)
    }
}

// MARK: - Section

struct LedgerSection<Content: View>: View {
    let title: String
    var count: Int? = nil
    var note: String = ""
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(verbatim: title)
                    .font(.system(size: Metrics.fsTitle + 2, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                if let count {
                    Text(verbatim: Display.number(count))
                        .font(.system(size: Metrics.fsUI).monospacedDigit())
                        .foregroundStyle(Palette.muted)
                }
                Spacer(minLength: 0)
            }
            .accessibilityAddTraits(.isHeader)
            if !note.isEmpty {
                Text(verbatim: note)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 680, alignment: .leading)
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A quiet sentence (the "Nothing." lines).
struct LedgerQuiet: View {
    let text: String
    var color: Color = Palette.muted
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: 720, alignment: .leading)
    }
}

/// A rounded panel, the page's one container shape.
struct LedgerCard<Content: View>: View {
    var padding: CGFloat = 0
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 0) { content }
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.panel2))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.line, lineWidth: 1))
    }
}

// MARK: - The row

/// The column heads above a ledger. The window is under 1,100 points wide beside the
/// sidebar, so "gives" sits under the name and "takes" under "have", as the web page
/// folds them at that width (view-ledger.css:79-90).
struct LedgerColumnHeads: View {
    let name: String
    let have: String
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Color.clear.frame(width: 34, height: 1)
            LedgerOverline(text: name).frame(maxWidth: .infinity, alignment: .leading)
            LedgerOverline(text: have).frame(maxWidth: .infinity, alignment: .leading)
            LedgerOverline(text: "Effort").frame(width: LedgerLayout.costWidth, alignment: .leading)
            Color.clear.frame(width: 12, height: 1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .accessibilityHidden(true)
    }
}

/// One line per thing the page keeps a row for. The whole line is one button; clicking it
/// opens the row's detail beneath it. A row in use says when it last ran where the
/// others say what they take, and has no effort chip: that effort is spent (NC-233).
// from static/app/ledger.js:130-165
struct LedgerRowView<Detail: View>: View {
    let row: LedgerRow
    let open: Bool
    var showsNote: Bool = true
    let toggle: () -> Void
    @ViewBuilder var detail: Detail
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: toggle) { head }
                .buttonStyle(.plain)
                .onHover { hovering = $0 }
                .accessibilityLabel([row.label, LedgerWords.tone(row.state.tone), row.have.says]
                    .filter { !$0.isEmpty }.joined(separator: ", "))
                .accessibilityHint(open ? "Closes this row" : "Opens this row")
                .accessibilityAddTraits(open ? .isSelected : [])
            if open {
                detail
                    .padding(.leading, 12 + 34 + 14)
                    .padding(.trailing, 16)
                    .padding(.top, 4)
                    .padding(.bottom, 16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Palette.panel2)
            }
        }
    }

    private var head: some View {
        let note = showsNote ? LedgerWords.note(row) : ""
        return HStack(alignment: .top, spacing: 14) {
            HStack(spacing: 7) {
                LedgerToneDot(tone: row.state.tone)
                Image(systemName: LedgerIcons.symbol(row.key))
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.muted)
                    .frame(width: 18)
            }
            .frame(width: 34, alignment: .leading)
            .padding(.top, 2)

            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: row.label)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                Text(verbatim: row.gives)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
            }
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                // "Nothing yet" only when the row has nothing else to say here: above a
                // status line such as "Not set" it only repeats it (ledger.js:152-155).
                if !row.have.says.isEmpty || note.isEmpty {
                    Text(verbatim: row.have.says.isEmpty ? "Nothing yet" : row.have.says)
                        .font(.system(size: Metrics.fsUI).monospacedDigit())
                        .foregroundStyle(row.have.says.isEmpty ? Palette.faint : Palette.ink)
                }
                if !note.isEmpty {
                    Text(verbatim: note)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(noteColor)
                }
                if !row.when.isEmpty {
                    Text(verbatim: row.when)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                } else if !row.takes.isEmpty {
                    Text("\(Text("TAKES").font(.system(size: 10, weight: .medium, design: .monospaced)).foregroundStyle(Palette.faint)) \(row.takes)")
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)

            Group {
                if row.when.isEmpty { LedgerCostChip(cost: row.cost) } else { Color.clear }
            }
            .frame(width: LedgerLayout.costWidth, alignment: .leading)
            .padding(.top, 2)

            Image(systemName: "chevron.down")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Palette.faint)
                .rotationEffect(.degrees(open ? 180 : 0))
                .frame(width: 12)
                .padding(.top, 3)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 11)
        .contentShape(Rectangle())
        .background(open ? Palette.panel2 : hovering ? Palette.hover : Color.clear)
    }

    private var noteColor: Color {
        switch row.state.tone {
        case "blocked": Palette.red
        case "behind": Palette.gold
        case "ready": Palette.ink
        default: Palette.muted
        }
    }
}

/// The five blocks of an open row, always in this order and always under these names,
/// so a person who has opened one row knows where to look in every other. A block with
/// nothing to say is left out.
// from static/app/ledger.js:170-186
struct LedgerDetailView<Now: View, Act: View, How: View, Other: View, Record: View>: View {
    var hasNow = false
    var hasAct = false
    var hasHow = false
    var hasOther = false
    var hasRecord = false
    @ViewBuilder var now: Now
    @ViewBuilder var act: Act
    @ViewBuilder var how: How
    @ViewBuilder var other: Other
    @ViewBuilder var record: Record

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if hasNow { block("Now") { now } }
            if hasAct { block("What you can do") { act } }
            if hasHow { block("How") { how } }
            if hasOther { block("The other way in") { other } }
            if hasRecord { block("What it added") { record } }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func block<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            LedgerOverline(text: title)
            content()
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.ink)
                .frame(maxWidth: 720, alignment: .leading)
        }
    }
}

/// A source's own instructions as what they are: numbered steps become an ordered list.
struct LedgerHelpView: View {
    let text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(LedgerWords.helpBlocks(text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .paragraph(let p):
                    Text(LedgerWords.inline(p))
                        .fixedSize(horizontal: false, vertical: true)
                case .steps(let steps):
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(steps.enumerated()), id: \.offset) { i, step in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(verbatim: "\(i + 1).")
                                    .monospacedDigit()
                                    .foregroundStyle(Palette.muted)
                                    .frame(minWidth: 18, alignment: .trailing)
                                Text(LedgerWords.inline(step)).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                case .bullets(let items):
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(verbatim: "•").foregroundStyle(Palette.muted)
                                Text(LedgerWords.inline(item)).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
        .font(.system(size: Metrics.fsUI))
        .textSelection(.enabled)
    }
}

// MARK: - Needs you, status rows

/// One thing the app cannot do without you. The sentence says what and why; the controls
/// under it are the fix, cheapest first.
// from static/app/ledger.js:190-197
struct LedgerNeedView<Acts: View>: View {
    let tone: String
    let says: String
    @ViewBuilder var acts: Acts

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                LedgerToneDot(tone: tone).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 1 }
                Text(verbatim: says)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            VStack(alignment: .leading, spacing: 8) { acts }
                .padding(.leading, 18)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// One read-only figure about the whole install, with what it is made of.
// from static/app/ledger.js:200-207
struct LedgerStatusRow<Value: View, Extra: View>: View {
    let label: String
    var detail: String = ""
    @ViewBuilder var value: Value
    @ViewBuilder var extra: Extra

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            Text(verbatim: label)
                .font(.system(size: Metrics.fsUI, weight: .medium))
                .foregroundStyle(Palette.muted)
                .frame(width: 150, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                value
                if !detail.isEmpty {
                    Text(verbatim: detail)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                extra
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}

extension LedgerStatusRow where Extra == EmptyView {
    init(label: String, detail: String = "", @ViewBuilder value: () -> Value) {
        self.label = label
        self.detail = detail
        self.value = value()
        self.extra = EmptyView()
    }
}

/// The confirmation at the foot of the page (the web page's toast), one line, 2.6 s.
struct LedgerFlash: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.onAction)
            .lineLimit(2)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(Capsule().fill(Palette.action))
            .shadow(color: .black.opacity(0.15), radius: 6, y: 2)
            .padding(.bottom, 16)
            .padding(.horizontal, 16)
            .accessibilityAddTraits(.updatesFrequently)
    }
}

/// A field note under a control (a refusal, "Not saved yet").
struct LedgerFieldNote: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsOverline + 1))
            .foregroundStyle(Display.Staleness.cool.color)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Controls

/// A secret the app keeps. Connect first; once one is stored it is shown by its last
/// characters only, and the only things you can do are Replace it or Remove it, because
/// "Save" over a masked value would lie about what the box holds.
// from static/app/ledger.js:279-338
struct LedgerCredentialBox: View {
    let act: LedgerAct
    let host: LedgerHost
    @State private var editing = false
    @State private var value = ""
    @State private var note = ""
    @State private var busy = false
    @State private var confirmRemove = false

    var body: some View {
        if act.isSet && !editing { stored } else { box }
    }

    private var stored: some View {
        HStack(spacing: 8) {
            Text(verbatim: act.label).foregroundStyle(Palette.muted)
            Text(verbatim: act.masked)
                .font(.system(size: Metrics.fsUI, design: .monospaced))
                .foregroundStyle(Palette.ink)
                .accessibilityLabel("\(act.label), stored, ending \(act.masked)")
            Button("Replace") {
                editing = true
                note = ""
            }
            .disabled(busy)
            Button("Remove…", role: .destructive) { confirmRemove = true }
                .disabled(busy)
        }
        .confirmationDialog("Remove \(act.label.lowercased())?", isPresented: $confirmRemove) {
            Button("Remove It for Good", role: .destructive) { Task { await remove() } }
            Button("Keep It", role: .cancel) {}
        } message: {
            Text("The app forgets the stored value. You can paste it again later.")
        }
    }

    private var box: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(verbatim: act.label).foregroundStyle(Palette.muted)
                SecureField(act.placeholder.isEmpty ? "Paste it here" : act.placeholder, text: $value)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 200, maxWidth: 320)
                    .onSubmit { Task { await connect() } }
                    .onChange(of: value) { note = "" }
                    .onExitCommand {
                        value = ""
                        note = ""
                    }
                Button(busy ? "Connecting…" : act.isSet ? "Replace" : "Connect") { Task { await connect() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy)
                if editing {
                    Button("Cancel") {
                        editing = false
                        value = ""
                        note = ""
                    }
                }
            }
            if !note.isEmpty { LedgerFieldNote(text: note) }
        }
    }

    private func connect() async {
        let problem = LedgerFormat.secretProblem(value, masked: act.masked, key: act.key)
        guard problem.isEmpty else {
            note = problem
            return
        }
        guard let client = host.app?.client, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let d = try await LedgerSettings.save(client, key: act.key, value: LedgerFormat.pasted(act.key, value))
            host.flash(d.warning ?? (act.isSet ? "\(act.label) replaced" : "\(act.label) connected"))
            value = ""
            editing = false
            note = ""
            await host.changed()
        } catch is CancellationError {
        } catch {
            note = LedgerSettings.sentence(error)
        }
    }

    private func remove() async {
        guard let client = host.app?.client, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let d = try await LedgerSettings.save(client, key: act.key, value: "")
            host.flash(d.warning ?? "\(act.label) removed")
            await host.changed()
        } catch is CancellationError {
        } catch {
            host.flash(LedgerSettings.sentence(error))
        }
    }
}

/// A setting that is not a secret, so it is shown back in full. Return saves, Esc puts
/// back what is stored, and an unsaved change says so (NC-218).
// from static/app/ledger.js:353-389
struct LedgerTextSetting: View {
    let act: LedgerAct
    let host: LedgerHost
    @State private var value: String
    @State private var note = ""
    @State private var busy = false

    init(act: LedgerAct, host: LedgerHost) {
        self.act = act
        self.host = host
        _value = State(initialValue: act.value)
    }

    private var trimmed: String { value.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var changed: Bool { trimmed != act.value }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(verbatim: act.label)
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                TextField(act.placeholder, text: $value)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 220, maxWidth: 380)
                    .autocorrectionDisabled()
                    .onSubmit { if changed { Task { await save() } } }
                    .onChange(of: value) { note = "" }
                    .onExitCommand {
                        value = act.value
                        note = ""
                    }
                Button(!trimmed.isEmpty || act.value.isEmpty ? "Save" : "Clear") { Task { await save() } }
                    .disabled(busy || !changed)
            }
            if !note.isEmpty || changed {
                LedgerFieldNote(text: note.isEmpty ? "Not saved yet. Press Save or Return." : note)
            }
        }
        // A fresh answer from the engine (after a save elsewhere) resets the box.
        .onChange(of: act.value) { _, new in value = new }
    }

    private func save() async {
        let problem = LedgerFormat.textProblem(act.key, value)
        guard problem.isEmpty else {
            note = problem
            return
        }
        guard let client = host.app?.client, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let d = try await LedgerSettings.save(client, key: act.key, value: trimmed)
            // `name` when the label is a sentence, so the line is not one.
            let name = act.name.isEmpty ? act.label : act.name
            host.flash(d.warning ?? (trimmed.isEmpty ? "\(name) cleared" : "\(name) saved"))
            note = ""
            await host.changed()
        } catch is CancellationError {
        } catch {
            note = LedgerSettings.sentence(error)
        }
    }
}

/// An on/off setting, saved the moment you choose, so it has no Save button. The web
/// page's two buttons are a Mac switch here.
// from static/app/ledger.js:394-413
struct LedgerSwitch: View {
    let act: LedgerAct
    let host: LedgerHost
    @State private var busy = false
    @State private var shown: Bool?

    var body: some View {
        Toggle(isOn: Binding(get: { shown ?? act.on }, set: { on in Task { await set(on) } })) {
            Text(verbatim: act.label)
        }
        .toggleStyle(.switch)
        .controlSize(.small)
        .disabled(busy)
        .onChange(of: act.on) { shown = nil }
    }

    private func set(_ on: Bool) async {
        guard on != act.on, let client = host.app?.client, !busy else { return }
        busy = true
        shown = on
        defer { busy = false }
        do {
            let d = try await LedgerSettings.save(client, key: act.key, value: on ? "1" : "0")
            host.flash(d.warning ?? "\(act.label): \(on ? "on" : "off")")
            await host.changed()
        } catch is CancellationError {
            shown = nil
        } catch {
            shown = nil
            host.flash(LedgerSettings.sentence(error))
        }
    }
}

/// A button that asks the engine to open an allowlisted folder or page.
struct LedgerOpenButton: View {
    let act: LedgerAct
    let host: LedgerHost
    var body: some View {
        Button {
            Task { await LedgerSettings.open(act.target, host: host) }
        } label: {
            Label(act.label, systemImage: "arrow.up.forward.square")
        }
    }
}

/// Lays controls out left to right, wrapping onto new lines when they do not fit.
struct LedgerFlow: Layout {
    var spacing: CGFloat = 8
    var lineSpacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, maxX: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(ProposedViewSize(width: width, height: nil))
            if x > 0 && x + size.width > width {
                y += lineHeight + lineSpacing
                x = 0
                lineHeight = 0
            }
            x += size.width + spacing
            maxX = max(maxX, x - spacing)
            lineHeight = max(lineHeight, size.height)
        }
        return CGSize(width: min(maxX, width), height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineHeight: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(ProposedViewSize(width: bounds.width, height: nil))
            if x > bounds.minX && x + size.width > bounds.maxX {
                y += lineHeight + lineSpacing
                x = bounds.minX
                lineHeight = 0
            }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
    }
}

enum LedgerLayout {
    /// The effort column, wide enough for "2 GB DOWNLOAD".
    static let costWidth: CGFloat = 118
}
