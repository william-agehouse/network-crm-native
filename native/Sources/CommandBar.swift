import AppKit
import SwiftUI

// ⌘K, Go > Search and Commands…: one box that jumps to a person, sends a question to Ask
// and runs commands — the omnibox of static/app/main.js:138-590, as a sheet. The state and
// the commands are in CommandBarModel.swift; this file draws them.
//
// Keys: ↑ ↓ move the highlight, Return runs it (or the first row), Escape or ⌘K closes.
// The mouse highlights what it is over without scrolling the list under it.

struct CommandBar: View {
    @AppEnvironment private var app
    @Environment(\.dismiss) private var dismiss
    @State private var model = CommandBarModel()
    @State private var focusRequest = 0

    static let size = CGSize(width: 640, height: 480)

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            searchLine
            Divider().overlay(Palette.line)
            CommandBarResults(model: model, run: run)
            if model.isCommandMode && !model.rows.isEmpty {
                Divider().overlay(Palette.line)
                footer
            }
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .background(Palette.panel)
        .background(closeKeys)
        .task {
            model.attach(app)
            takeSnapshotHint()
            await model.start()
        }
    }

    // MARK: Search line

    private var searchLine: some View {
        @Bindable var model = model
        return HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(Palette.muted)
                .accessibilityHidden(true)
            contextChip
            CmdSearchField(
                text: $model.query, placeholder: "Search a name · > for commands", focusRequest: focusRequest,
                onMove: { model.move($0) }, onSubmit: { run(model.current) }, onCancel: { dismiss() })
                .frame(maxWidth: .infinity)
            Text(verbatim: "esc")
                .font(.system(size: Metrics.fsOverline, weight: .medium))
                .foregroundStyle(Palette.muted)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Palette.lineStrong))
                .help("Close (Escape or ⌘K)")
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 16)
        .frame(height: 54)
    }

    /// Where you are: the open person by name and photo, else the section (main.js:497-503).
    private var contextChip: some View {
        Button {
            focusRequest += 1
        } label: {
            HStack(spacing: 5) {
                if let p = model.contextPerson {
                    CmdAvatar(name: p.name, photo: p.photo, size: 18, client: app.client)
                }
                Text(verbatim: model.contextText)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: 160, alignment: .leading)
                    .fixedSize(horizontal: true, vertical: false)
            }
            .font(.system(size: Metrics.fsUI, weight: .medium))
            .foregroundStyle(Palette.ink)
            .padding(.leading, model.contextPerson == nil ? 8 : 3)
            .padding(.trailing, 8)
            .frame(height: 24)
            .background(Capsule().fill(Palette.panel2))
            .overlay(Capsule().strokeBorder(Palette.line))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("On \(model.contextText). Press ⌘K to jump anywhere.")
    }

    private var footer: some View {
        HStack(spacing: 4) {
            Image(systemName: "arrow.up")
            Image(systemName: "arrow.down")
            Text("move ·")
            Image(systemName: "return")
            Text("run · type a name, a city or a topic for more")
            Spacer(minLength: 0)
        }
        .font(.system(size: Metrics.fsOverline))
        .foregroundStyle(Palette.muted)
        .padding(.horizontal, 16)
        .frame(height: 30)
        .accessibilityElement(children: .combine)
    }

    /// Escape and ⌘K close the bar wherever focus is (⌘K toggles, main.js:426-435).
    private var closeKeys: some View {
        ZStack {
            Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            Button("Close") { dismiss() }.keyboardShortcut("k", modifiers: .command)
        }
        .opacity(0)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    // MARK: Running

    private func run(_ row: CmdRow?) {
        guard let row else { return }
        if case .meaning = row {
            Task { await model.searchByMeaning() }
            focusRequest += 1
            return
        }
        // A group message with no page to open leads nowhere (main.js:466-472).
        guard let action = CommandBarModel.action(for: row) else { return }
        CommandBarRunner.run(action, app: app)
    }

    /// `--snapshot`: "command:<text>" opens the bar with that text typed.
    private func takeSnapshotHint() {
        guard let hint = app.snapshotHint, hint.hasPrefix("command:") else { return }
        app.snapshotHint = nil
        model.query = String(hint.dropFirst("command:".count))
    }
}

// MARK: - Results

private struct CommandBarResults: View {
    let model: CommandBarModel
    let run: (CmdRow?) -> Void
    @AppEnvironment private var app

    var body: some View {
        let rows = model.rows
        let firstCmd = rows.firstIndex { if case .cmd = $0 { true } else { false } }
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if !model.hint.isEmpty && !model.isCommandMode {
                        note(model.hint)
                    }
                    // The list used to vanish on no match, which reads as a broken box
                    // (main.js:532-536). Only ">" mode can get here.
                    if rows.isEmpty {
                        note("No command matches. Clear the box to see them all.")
                    }
                    if !model.isCommandMode && !model.people.isEmpty {
                        header("People")
                    }
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                        if case .convo(_, 0) = row { header(model.convoHeader) }
                        if index == firstCmd { header("Commands") }
                        CommandBarRow(row: row, selected: index == model.selection, meaningBusy: model.meaningBusy,
                                      client: app.client)
                            .id(row.id)
                            .onHover { if $0 { model.hover(index) } }
                            .onTapGesture { run(row) }
                            .accessibilityAction { run(row) }
                    }
                }
                .padding(6)
            }
            .scrollContentBackground(.hidden)
            .onChange(of: model.selection) {
                guard model.keyboardMoved, rows.indices.contains(model.selection) else { return }
                proxy.scrollTo(rows[model.selection].id)
            }
        }
    }

    private func header(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.system(size: Metrics.fsOverline, weight: .semibold))
            .kerning(0.4)
            .foregroundStyle(Palette.muted)
            .padding(.horizontal, 10)
            .padding(.top, 8)
            .padding(.bottom, 3)
            .accessibilityAddTraits(.isHeader)
    }

    private func note(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
    }
}

private struct CommandBarRow: View {
    let row: CmdRow
    let selected: Bool
    let meaningBusy: Bool
    let client: EngineClient

    var body: some View {
        HStack(spacing: Metrics.gapSmall) {
            leading
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: title)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .truncationMode(.tail)
                if !meta.isEmpty {
                    Text(verbatim: meta)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.horizontal, Metrics.gapSmall)
        .padding(.vertical, 4)
        .frame(minHeight: meta.isEmpty ? 34 : Metrics.rowHeight)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(selected ? Palette.accentSoft : .clear))
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }

    @ViewBuilder private var leading: some View {
        switch row {
        case .person(let p):
            CmdAvatar(name: p.name, photo: p.photo, size: 24, client: client)
        case .convo(let c, _):
            CmdGlyph(symbol: Channel(rawValue: c.channel).map { Display.channelSymbol($0) } ?? "bubble.left")
        case .cmd(let c):
            CmdGlyph(symbol: c.symbol)
        case .meaning:
            CmdGlyph(symbol: "brain")
        case .ask:
            CmdGlyph(symbol: "bubble.left.and.text.bubble.right")
        }
    }

    @ViewBuilder private var trailing: some View {
        switch row {
        case .meaning where meaningBusy:
            ProgressView().controlSize(.mini)
        case .meaning, .ask:
            Image(systemName: "return").font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.muted)
        default:
            Text(verbatim: kind)
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
                .lineLimit(1)
                .fixedSize()
        }
    }

    private var title: String {
        switch row {
        case .person(let p): p.name
        case .convo(let c, _): CommandBarModel.convoTitle(c)
        case .cmd(let c): c.label
        case .meaning: meaningBusy ? "Searches by meaning…" : "Search conversations by meaning"
        case .ask(let q): "Ask: “\(q)”"
        }
    }

    private var meta: String {
        switch row {
        case .person(let p):
            let role = Display.roleString(position: p.position, company: p.company)
            return role.isEmpty ? p.homeMetro : role
        case .convo(let c, _): return CommandBarModel.plainSnippet(c.snippet)
        case .cmd: return ""
        case .meaning: return "Finds the words that mean the same"
        case .ask: return "Asks your whole network"
        }
    }

    private var kind: String {
        switch row {
        case .person: "open"
        case .convo(let c, _): Display.relDate(c.ts)
        case .cmd(let c): c.hint
        case .meaning, .ask: ""
        }
    }
}
