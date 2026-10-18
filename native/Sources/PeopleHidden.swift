import SwiftUI

// "Hidden": the names every import skips and every view hides — spam, bots, anyone you
// never want to see. A toolbar button with the count, opening a popover, as the web page's
// pill does (people.js:281-356, 862-864; moved there from Settings by NC-003).

/// The toolbar button: "Hidden" and how many names are on the list.
struct PeopleHiddenButton: View {
    let extras: PeopleExtrasStore
    @Binding var isPresented: Bool
    let onChange: () -> Void

    var body: some View {
        let n = extras.hidden?.count ?? 0
        Button {
            isPresented.toggle()
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "eye.slash")
                Text("Hidden")
                if n > 0 {
                    Text(verbatim: Display.number(n))
                        .font(.system(size: 10, weight: .semibold).monospacedDigit())
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(Palette.lineStrong))
                }
            }
        }
        // people.js:863
        .help("Hidden from every view, search and AI answer. Their rows stay in your data — hiding is a filter, not a delete.")
        .accessibilityLabel(n > 0 ? "Hidden names, \(n)" : "Hidden names")
        .popover(isPresented: $isPresented, arrowEdge: .bottom) {
            PeopleHiddenPanel(extras: extras, onChange: onChange)
        }
    }
}

/// The panel itself. Also drawn on its own by `--snapshot` (a popover is its own window).
struct PeopleHiddenPanel: View {
    let extras: PeopleExtrasStore
    /// Called after a name is added (the engine hides matching people at once, so the
    /// table and the counts must refresh) or removed.
    let onChange: () -> Void
    var initialName = ""

    @AppEnvironment private var app
    @State private var name = ""
    @State private var line: (text: String, isError: Bool)?
    @FocusState private var fieldFocused: Bool

    private var typed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var duplicate: Bool { extras.isHidden(name) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Hidden contacts")
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                Text("Hidden everywhere. Every import obeys this list.")
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
            }

            HStack(spacing: 6) {
                TextField("Full name to hide", text: $name)
                    .textFieldStyle(.roundedBorder)
                    .focused($fieldFocused)
                    .onSubmit(add)
                    .accessibilityLabel("Full name to hide")
                Button("Add", action: add)
                    .disabled(typed.isEmpty || duplicate || extras.hiddenBusy)
            }
            .controlSize(.regular)
            if duplicate {
                Text("This name is already hidden.")
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Display.Staleness.cool.color)
            } else if let line {
                Text(verbatim: line.text)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(line.isError ? Palette.danger : Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()
            entries
            Text("Removing a name lets imports collect it again. A person hidden already stays in the Excluded view until you unexclude them there.")
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(width: 340)
        .task {
            if !initialName.isEmpty && name.isEmpty { name = initialName }
            await extras.loadHidden(client: app.client)
            app.markSettled()
        }
        .onAppear { fieldFocused = true }
    }

    @ViewBuilder private var entries: some View {
        if let list = extras.hidden {
            if let error = extras.hiddenError {
                Text(verbatim: error)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.danger)
                    .fixedSize(horizontal: false, vertical: true)
            } else if list.isEmpty {
                Text("No hidden names.")
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(list) { entry in row(entry) }
                    }
                }
                .frame(maxHeight: 260)
                .fixedSize(horizontal: false, vertical: list.count <= 7)
            }
        } else {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Reading the hidden names…").foregroundStyle(Palette.muted)
            }
            .font(.system(size: Metrics.fsUI))
        }
    }

    private func row(_ entry: ExcludeEntry) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: entry.name)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if !entry.inDB {
                    Text("not in your contacts")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
            Spacer(minLength: 8)
            Button("Remove") { remove(entry.name) }
                .controlSize(.small)
                .disabled(extras.hiddenBusy)
                .accessibilityLabel("Stop hiding \(entry.name)")
        }
        .padding(.vertical, 4)
    }

    private func add() {
        let n = typed
        guard !n.isEmpty, !duplicate, !extras.hiddenBusy else { return }
        Task {
            do {
                let said = try await extras.addHidden(n, client: app.client)
                name = ""
                line = (said, false)
                onChange()
            } catch {
                // What was typed stays in the field.
                line = ("\(n) was not hidden. \(PersonStore.say(error))", true)
            }
        }
    }

    private func remove(_ n: String) {
        Task {
            let said = await extras.removeHidden(n, client: app.client)
            line = (said.text, !said.ok)
            onChange()
        }
    }
}
