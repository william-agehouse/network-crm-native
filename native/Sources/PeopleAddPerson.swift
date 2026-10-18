import SwiftUI

// Add someone new: a name and, optionally, a company (static/app/people.js:925-981).
// Before creating anyone it asks the engine for that exact name, because the engine itself
// never refuses a duplicate (contract §3): an exact same name (ignoring case) offers to open
// the person already there instead. Opened by the toolbar button, ⌘N, File > Add Person…
// and ⌘K, all through `AppModel.showAddPerson`.

struct PeopleAddPersonSheet: View {
    /// Called with the new (or existing) person's id once the sheet is done.
    let onOpen: (Int) -> Void
    /// Called after a person was created, with the line to show ("Added Ada Lovelace").
    let onAdded: (String) -> Void
    var initialName = ""
    var initialCompany = ""

    @AppEnvironment private var app
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var company = ""
    /// An existing person with exactly this name (people.js:929, :950).
    @State private var match: PersonSummary?
    @State private var busy = false
    @State private var error: String?
    @FocusState private var nameFocused: Bool

    private var typed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New person")
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
                .foregroundStyle(Palette.ink)

            Form {
                TextField("Name", text: $name, prompt: Text("Their name"))
                    .focused($nameFocused)
                    .onChange(of: name) { match = nil; error = nil }
                TextField("Company", text: $company, prompt: Text("Company (optional)"))
            }
            .formStyle(.columns)
            .disabled(busy)

            if let match {
                VStack(alignment: .leading, spacing: 8) {
                    Text(verbatim: "“\(match.name)” is already in your network.")
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.ink)
                    HStack(spacing: 8) {
                        Button("Open Them") {
                            onOpen(match.id)
                            dismiss()
                        }
                        // The web page's primary button here (people.js:973), so Return adds.
                        Button("Add Anyway") { Task { await create() } }
                            .disabled(busy)
                            .keyboardShortcut(.defaultAction)
                    }
                    .controlSize(.small)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.goldSoft))
            }
            if let error {
                Text(verbatim: error)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                if busy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add") { Task { await add() } }
                    .keyboardShortcut(match == nil ? .defaultAction : nil)
                    .disabled(typed.isEmpty || busy || match != nil)
            }
        }
        .padding(20)
        .frame(width: 420)
        .onAppear {
            if name.isEmpty && !initialName.isEmpty { name = initialName }
            if company.isEmpty && !initialCompany.isEmpty { company = initialCompany }
            nameFocused = true
        }
        .task {
            // --snapshot of the "already in your network" state: run the check at once.
            if !initialName.isEmpty { await add(checkOnly: true) }
            app.markSettled()
        }
    }

    /// people.js:945-954: warn before creating a second person with the exact same name.
    /// If the check itself fails, adding is not blocked.
    private func add(checkOnly: Bool = false) async {
        let n = typed
        guard !n.isEmpty, !busy else { return }
        busy = true
        error = nil
        let same = await Self.existing(named: n, client: app.client)
        busy = false
        if let same {
            match = same
            return
        }
        if !checkOnly { await create() }
    }

    private func create() async {
        let n = typed
        guard !n.isEmpty else { return }
        busy = true
        defer { busy = false }
        do {
            let id = try await Self.create(name: n, company: company, client: app.client)
            onAdded("Added \(n)")
            onOpen(id)
            dismiss()
        } catch {
            // What was typed stays in the fields.
            self.error = "\(n) was not added. \(PersonStore.say(error))"
        }
    }

    // The two engine steps, apart from the view so --selftest-writes runs exactly these.

    /// Someone already called exactly `name`, ignoring case and outer spaces, or nil (also
    /// when the check could not be made: it must never block adding).
    static func existing(named name: String, client: EngineClient) async -> PersonSummary? {
        let n = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty else { return nil }
        let found = try? await client.get(
            "/api/contacts", query: ["q": n, "sort": "closeness", "limit": "20000"], as: ContactsResponse.self)
        return found?.contacts.first {
            $0.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == n.lowercased()
        }
    }

    /// people.js:935-944: POST the name, then the company as a field (a failure there does
    /// not undo the person). Returns the new person's id.
    static func create(name: String, company: String, client: EngineClient) async throws -> Int {
        let n = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let made = try await client.post("/api/contacts", body: ["name": JSONValue.string(n)], as: CreatedID.self)
        let c = company.trimmingCharacters(in: .whitespacesAndNewlines)
        if !c.isEmpty {
            _ = try? await client.post(
                "/api/contact/\(made.id)/field", body: ["field": "company", "value": .string(c)] as [String: JSONValue],
                as: WriteResult.self)
        }
        return made.id
    }
}
