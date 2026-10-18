import SwiftUI

// The review step and the receipt of the paste sheet: what the paste held, the links in
// it, one card per proposed person, and what a commit actually did. Words, order and
// rules come from static/app/intake.js (ReportPanel, LinkPanel, ProposalRow, CommitReceipt).

struct PasteReviewPhase: View {
    let store: PasteStore

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let receipt = store.receipt { PasteReceiptView(receipt: receipt) }
            if let plan = store.plan {
                PasteReportPanel(report: plan.report)
                PasteLinkPanel(store: store, urls: plan.allowedLookupUrls)
                let rows = store.rows
                if rows.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Nobody was found in that text")
                            .font(.system(size: Metrics.fsTitle, weight: .semibold))
                        Text("Nothing was written. Go back and paste a list with one person per line.")
                            .font(.system(size: Metrics.fsUI))
                            .foregroundStyle(Palette.muted)
                        Text("A line needs a name, a LinkedIn address, an email or a phone number.")
                            .font(.system(size: Metrics.fsOverline + 1))
                            .foregroundStyle(Palette.muted)
                    }
                    .padding(.vertical, 12)
                } else {
                    ForEach(rows) { row in PasteProposalCard(store: store, row: row) }
                }
            }
        }
    }
}

// MARK: - What the paste held (intake.js:155-203)

private struct PasteReportPanel: View {
    let report: PasteReport
    @State private var showDropped = false

    var body: some View {
        let lost = report.accounting.linesUnexplained + report.accounting.recordsUnexplained
        let declined = report.factsDeclinedByField.keys.sorted()
        VStack(alignment: .leading, spacing: 8) {
            SetupSectionLabel(text: "What the paste held")
            HStack(spacing: 18) {
                number(report.wouldCreate, "to add")
                number(report.wouldEnrich, "already known")
                number(report.blocked, "refused")
                Text("\(SetupWords.people(report.recordsParsed)) read from \(report.linesSeen) lines")
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
            }
            if !report.degraded.isEmpty { SetupLine(text: report.degraded, tone: .warn) }
            if lost > 0 {
                SetupLine(text: "\(lost) lines are unaccounted for. The parse lost something it did not report — treat the list below as incomplete.", tone: .bad)
            }
            SetupBullets(lines: report.sentences)
            if !declined.isEmpty {
                SetupLine(text: "Facts were declined for \(declined.joined(separator: ", ")). Intake writes identity only, never a city, a topic, a role or a blurb.", tone: .plain, symbol: nil)
            }
            if !report.linkedinUrlNote.isEmpty {
                SetupLine(text: report.linkedinUrlNote, tone: .plain, symbol: nil)
            }
            if let spend = report.model.spend {
                SetupLine(
                    text: "The model call cost $\(String(format: "%.4f", spend.usd)) — \(spend.inputTokens) in, \(spend.outputTokens) out, \(spend.model.isEmpty ? "model" : spend.model).",
                    tone: .plain, symbol: nil)
            }
            if !report.skippedDetail.isEmpty {
                let n = report.skippedDetail.count
                DisclosureGroup(isExpanded: $showDropped) {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(report.skippedDetail.enumerated()), id: \.offset) { _, d in
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text(d.line > 0 ? "Line \(d.line)" : "—")
                                    .font(.system(size: Metrics.fsOverline + 1).monospacedDigit())
                                    .foregroundStyle(Palette.muted)
                                    .frame(width: 54, alignment: .leading)
                                Text([d.name.isEmpty ? d.text : d.name, d.reason].filter { !$0.isEmpty }.joined(separator: " — "))
                                    .font(.system(size: Metrics.fsUI))
                                    .foregroundStyle(Palette.ink)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    .padding(.top, 6)
                } label: {
                    Text("\(n) \(n == 1 ? "entry was" : "entries were") not proposed")
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                }
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
    }

    private func number(_ n: Int, _ label: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text("\(n)")
                .font(.system(size: Metrics.fsTitle, weight: .semibold).monospacedDigit())
                .foregroundStyle(Palette.ink)
            Text(label)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
        }
    }
}

// MARK: - Links in the paste: opening one is a request to that site (intake.js:298-327)

private struct PasteLinkPanel: View {
    let store: PasteStore
    let urls: [String]

    var body: some View {
        let webOn = store.status?.webLookup == true
        if !urls.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SetupSectionLabel(text: "Links in your paste")
                if !webOn {
                    SetupLine(
                        text: "Web lookup is off, so no link was opened. Turn on “Let Intake read a web page” in Settings to allow it. Pasting names, LinkedIn addresses and emails works fully without it.",
                        tone: .plain, symbol: nil)
                }
                ForEach(urls, id: \.self) { u in
                    HStack(spacing: 8) {
                        Text(verbatim: u)
                            .font(.system(size: Metrics.fsUI, design: .monospaced))
                            .foregroundStyle(Palette.ink)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                        Spacer(minLength: 8)
                        if let page = store.pages.first(where: { $0.url == u }) {
                            Text("Read \(page.bytes) bytes from \(page.host)")
                                .font(.system(size: Metrics.fsOverline + 1))
                                .foregroundStyle(Palette.muted)
                        } else if webOn {
                            Button(store.busy == .lookup ? "Reading…" : "Read this page") {
                                Task { await store.lookup(u) }
                            }
                            .controlSize(.small)
                            .disabled(store.busy != .none)
                            .help("Fetches this one address from the web")
                        }
                    }
                }
                if !store.pages.isEmpty {
                    let n = store.pages.count
                    SetupLine(
                        text: "\(n) \(n == 1 ? "page" : "pages") were read and have NOT been sent to a model. This text is treated as information about people and never as an instruction. Press “Read it with the model” to use it.",
                        tone: .plain, symbol: nil)
                }
            }
        }
    }
}

// MARK: - One person, as proposed, editable (intake.js:205-296)

private struct PasteProposalCard: View {
    let store: PasteStore
    let row: PasteRow

    var body: some View {
        let p = row.p
        let v = PasteFields.verdict(p.verdict)
        let needsName = p.verdict == "new" && !row.item.name.trimmingCharacters(in: .whitespaces).isEmpty && !row.item.nameConfirmed
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                SetupTag(text: v.label, tone: v.tone)
                Text(row.item.name.isEmpty ? "No name" : row.item.name)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(row.item.name.isEmpty ? Palette.muted : Palette.ink)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Button(row.skip ? "Include" : "Skip") { store.setSkip(p.ref, !row.skip) }
                    .controlSize(.small)
                    .help(row.skip ? "Put this person back in the batch" : "Leave this person out")
            }
            if !p.match.reason.isEmpty {
                Text(p.match.reason)
                    .font(.system(size: Metrics.fsOverline + 1))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(row.blocked.enumerated()), id: \.offset) { _, b in
                SetupLine(text: b, tone: p.verdict == "new" ? .warn : .bad)
            }
            if !row.skip {
                PasteFieldGrid(store: store, row: row)
                if needsName {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Button("Yes, they are called “\(row.item.name)”") {
                            store.setEdit(p.ref, "name_confirmed", .bool(true))
                        }
                        .controlSize(.small)
                        Text("Correct the name above first if it is wrong. This is the one thing the server will not decide for you.")
                            .font(.system(size: Metrics.fsOverline + 1))
                            .foregroundStyle(Palette.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if p.verdict == "new" && row.item.nameConfirmed && !row.item.name.trimmingCharacters(in: .whitespaces).isEmpty {
                    SetupLine(
                        text: "The name is confirmed." + (store.hasTypedName(p.ref) ? " You typed it." : ""),
                        tone: .ok, symbol: "checkmark")
                }
            }
            ForEach(Array(p.warnings.enumerated()), id: \.offset) { _, w in
                SetupLine(text: w, tone: .warn)
            }
            if let first = p.factsDeclined.first {
                SetupLine(
                    text: "Not saved: \(p.factsDeclined.map(\.field).joined(separator: ", ")). \(first.why)",
                    tone: .plain, symbol: nil)
            }
        }
        .padding(14)
        .opacity(row.skip ? 0.6 : 1)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall)
            .strokeBorder(row.blocked.isEmpty || row.skip ? Palette.line : SetupTone.warn.color.opacity(0.5), lineWidth: 1))
    }
}

/// A field with no value and nothing to say about it is not drawn: eight empty boxes per
/// person would turn a review into a form (intake.js:259-261).
private struct PasteFieldGrid: View {
    let store: PasteStore
    let row: PasteRow

    var body: some View {
        let shown = PasteFields.all.filter { f in
            !row.item[f.key].isEmpty || !PasteStore.fieldNote(row.p, f.key).isEmpty
        }
        if !shown.isEmpty {
            // One label width for every card, so the boxes line up down the whole list.
            VStack(alignment: .leading, spacing: 6) {
                ForEach(shown, id: \.key) { f in
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(f.label)
                            .font(.system(size: Metrics.fsUI))
                            .foregroundStyle(Palette.muted)
                            .frame(width: 84, alignment: .trailing)
                        PasteFieldValue(store: store, row: row, key: f.key, label: f.label)
                    }
                }
            }
        }
    }
}

private struct PasteFieldValue: View {
    let store: PasteStore
    let row: PasteRow
    let key: String
    let label: String

    var body: some View {
        let note = PasteStore.fieldNote(row.p, key)
        let value = row.item[key]
        VStack(alignment: .leading, spacing: 2) {
            if PasteStore.writable(row.p, key) {
                TextField(label, text: Binding(
                    get: { row.item[key] },
                    set: { store.setEdit(row.p.ref, key, .string($0)) }))
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: Metrics.fsUI))
                    .accessibilityLabel(label)
            } else {
                Text(value.isEmpty ? "—" : value)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.ink)
                    .textSelection(.enabled)
            }
            if !note.isEmpty {
                Text(note)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - What actually happened (intake.js:329-360)

struct PasteReceiptView: View {
    let receipt: PasteReceipt
    @AppEnvironment private var app

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SetupSectionLabel(text: receipt.wroteAnything ? "Saved" : "Nothing was written")
            SetupBullets(lines: receipt.sentences)
            let un = receipt.accounting.unaccounted
            if un != 0 {
                SetupLine(text: "\(un) submitted \(un == 1 ? "row" : "rows") are unaccounted for in this receipt.", tone: .bad)
            }
            if !receipt.outcomes.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(receipt.outcomes) { o in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            if let id = o.contactID {
                                Button(o.name.isEmpty ? "This person" : o.name) {
                                    app.openPerson(id)
                                    app.setupFlow = nil
                                }
                                .buttonStyle(.link)
                                .help("Open their page")
                            } else {
                                Text(o.name.isEmpty ? "No name" : o.name)
                                    .font(.system(size: Metrics.fsUI, weight: .medium))
                            }
                            Text(Self.describe(o))
                                .font(.system(size: Metrics.fsUI))
                                .foregroundStyle(o.refused.isEmpty ? Palette.muted : Palette.red)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.top, 2)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall)
            .fill(receipt.wroteAnything ? Palette.greenSoft.opacity(0.5) : Palette.panel2))
    }

    // from static/app/intake.js:351-355
    static func describe(_ o: PasteOutcome) -> String {
        if !o.said.isEmpty { return o.said }   // the engine's own words; the rest serves an older engine
        if !o.refused.isEmpty { return o.refused }
        let parts = [
            o.wrote.isEmpty ? "" : "Wrote \(o.wrote.joined(separator: ", ")).",
            o.kept.isEmpty ? "" : "Kept \(o.kept.joined(separator: ", ")).",
        ].filter { !$0.isEmpty }
        return parts.isEmpty ? "Nothing to change." : parts.joined(separator: " ")
    }
}
