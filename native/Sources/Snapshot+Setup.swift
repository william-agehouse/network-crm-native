import SwiftUI

// Extra --snapshot scenes for the setup sheets (Full Disk Access, Gmail, paste a list). Owned by that screen's files; Snapshot.run adds
// these to its own list. `firstID` is the closest person, for scenes that need someone.
//
// Three kinds of picture:
// - the sheet as the engine on NC_NATIVE_PORT reports it (read-only routes only);
// - the paste review, read from a made-up sample on the empty scratch engine (8768), with
//   the plain read only and only when that engine has no AI key;
// - states no read can reach without pressing a button that installs something, signs in
//   or writes (a blocked helper, connected Gmail addresses, a saved batch), drawn from the
//   made-up values below. Every name, address and path in them is invented.

extension Snapshot {
    static func setupScenes(firstID: Int?) -> [Scene] {
        let fda = NSSize(width: 620, height: 660)
        let gmail = NSSize(width: 640, height: 680)
        let paste = NSSize(width: 780, height: 720)
        return [
            Scene(name: "setup-fda", view: { _ in AnyView(SetupFlowView(flow: .fullDiskAccess)) }, size: fda) { _ in },
            Scene(name: "setup-fda-blocked", view: { _ in
                AnyView(FullDiskAccessSheet(store: FullDiskAccessStore(preview: SetupSamples.fdaBlocked, checked: true)))
            }, size: fda) { _ in },
            // The whole blocked sheet in one tall picture (the buttons sit below the fold),
            // with the answer "Check now" gives pinned under the title.
            Scene(name: "setup-fda-blocked-full", view: { _ in
                AnyView(FullDiskAccessSheet(store: FullDiskAccessStore(
                    preview: SetupSamples.fdaBlocked, output: SetupSamples.installerOutput, checked: true,
                    notice: SetupNotice(text: "Still blocked: iMessage.", tone: .warn)))
                    .tallForSnapshot(width: 620, height: 1360))
            }, size: NSSize(width: 620, height: 1360)) { _ in },
            Scene(name: "setup-fda-full", view: { _ in
                AnyView(SetupFlowView(flow: .fullDiskAccess).tallForSnapshot(width: 620, height: 800))
            }, size: NSSize(width: 620, height: 800)) { _ in },
            // NC-223: every source copies, but the app carries a newer helper than the
            // installed one. No read reaches this on a scratch engine without an older
            // helper installed, so it is drawn from made-up values too.
            Scene(name: "setup-fda-newer", view: { _ in
                AnyView(FullDiskAccessSheet(store: FullDiskAccessStore(preview: SetupSamples.fdaNewer)))
            }, size: fda) { _ in },
            Scene(name: "setup-fda-offer", view: { _ in
                AnyView(FullDiskAccessSheet(store: FullDiskAccessStore(preview: SetupSamples.fdaOffer)))
            }, size: fda) { _ in },
            // Sized like the sheet with no address set up, which is what the real engine reports.
            Scene(name: "setup-gmail", view: { _ in AnyView(SetupFlowView(flow: .gmail)) },
                  size: NSSize(width: 640, height: 330)) { _ in },
            Scene(name: "setup-gmail-accounts", view: { _ in
                AnyView(GmailSheet(store: GmailStore(preview: SetupSamples.gmail)))
            }, size: gmail) { _ in },
            Scene(name: "setup-gmail-accounts-full", view: { _ in
                AnyView(GmailSheet(store: GmailStore(preview: SetupSamples.gmail))
                    .tallForSnapshot(width: 640, height: 1330))
            }, size: NSSize(width: 640, height: 1330)) { _ in },
            Scene(name: "setup-paste", view: { _ in AnyView(SetupFlowView(flow: .paste)) }, size: paste) { _ in },
            Scene(name: "setup-paste-review", port: SelfTest.scratchPort, view: { _ in
                AnyView(SetupFlowView(flow: .paste))
            }, size: paste) { app in app.snapshotHint = "paste-review" },
            // The review's person cards, which sit below the fold in the sheet: the same
            // made-up sample, drawn unclipped.
            Scene(name: "setup-paste-cards", port: SelfTest.scratchPort, view: { _ in AnyView(PasteSnapshotCards()) },
                  size: NSSize(width: 780, height: 1800)) { _ in },
            Scene(name: "setup-paste-draft", view: { _ in
                AnyView(PasteSheet(store: PasteStore(previewStatus: SetupSamples.intakeStatus, draft: SetupSamples.draft)))
            }, size: paste) { _ in },
            Scene(name: "setup-paste-done", view: { _ in
                AnyView(PasteSheet(store: PasteStore(previewStatus: SetupSamples.intakeStatus, plan: SetupSamples.emptyPlan, receipt: SetupSamples.receipt)))
            }, size: paste) { _ in },
        ]
    }
}

extension View {
    /// A sheet drawn whole, taller than any screen: the height cap lifted, filling the
    /// scene's window below its title bar (the window itself never resizes).
    func tallForSnapshot(width: CGFloat, height: CGFloat) -> some View {
        environment(\.setupSheetUnclipped, true)
            .frame(maxWidth: width, maxHeight: height, alignment: .top)
    }
}

/// Made-up values for the pictures of states the engine cannot be asked to show.
@MainActor
enum SetupSamples {
    static let labels = ["iMessage", "WhatsApp", "Contacts", "Calendar"]

    static func source(_ label: String, _ outcome: String, matched: Int?, bytes: Int? = nil) -> FullDiskAccessSource {
        var s = FullDiskAccessSource()
        s.label = label
        s.outcome = outcome
        s.matched = matched
        s.bytes = bytes
        return s
    }

    static var fdaBlocked: FullDiskAccessStatus {
        var s = FullDiskAccessStatus()
        s.state = "blocked"
        s.available = true
        s.installed = true
        s.binary = "/Users/you/Library/Application Support/NetworkCRM/bin/networkcrm-imessage-sync"
        s.binaryDir = "/Users/you/Library/Application Support/NetworkCRM/bin"
        s.binaryExists = true
        s.sources = [
            source("iMessage", "blocked", matched: 0),
            source("WhatsApp", "current", matched: 3, bytes: 2_400_000),
            source("Contacts", "copied", matched: 1, bytes: 90_000),
            source("Calendar", "absent", matched: 0),
        ]
        s.blocked = ["iMessage"]
        s.ok = ["WhatsApp", "Contacts"]
        s.copies = labels
        s.ranAgoSeconds = 140
        s.staleHours = 24
        s.waits = ["install": 300, "uninstall": 300, "recheck": 65]
        return s
    }

    /// Made up, in the shape install_imessage_sync.sh prints on the blocked path.
    static let installerOutput = """
        Installing the Network CRM sync helper…
        Helper already current: /Users/you/Library/Application Support/NetworkCRM/bin/networkcrm-imessage-sync
        LaunchAgent loaded: runs every 120 s.
        iMessage still needs Full Disk Access.
        """

    static var fdaNewer: FullDiskAccessStatus {
        var s = fdaBlocked
        s.state = "outdated"
        s.sources = [
            source("iMessage", "current", matched: 12),
            source("WhatsApp", "current", matched: 3, bytes: 2_400_000),
            source("Contacts", "copied", matched: 1, bytes: 90_000),
            source("Calendar", "absent", matched: 0),
        ]
        s.blocked = []
        s.ok = ["iMessage", "WhatsApp", "Contacts"]
        s.newerInApp = true
        s.newerSays = "It writes less to your disk: it no longer rewrites its own bookkeeping file every two minutes when nothing changed, and its log stays under 1 MB."
        return s
    }

    static var fdaOffer: FullDiskAccessStatus {
        var s = FullDiskAccessStatus()
        s.state = "not_installed"
        s.available = true
        s.copies = labels
        s.staleHours = 24
        return s
    }

    static func account(_ email: String, creds: Bool, token: Bool, expired: Bool = false) -> GmailAccount {
        var a = GmailAccount()
        a.email = email
        a.hasCredentials = creds
        a.hasToken = token
        a.signInExpired = expired
        return a
    }

    static var gmail: GmailStatus {
        var s = GmailStatus()
        s.accounts = [
            account("ada@example.com", creds: false, token: false),
            account("grace@example.com", creds: true, token: false),
            account("alan@example.com", creds: true, token: true),
            account("hopper@example.com", creds: true, token: true, expired: true),
        ]
        s.configured = true
        s.needsFile = ["ada@example.com"]
        s.needsSignIn = ["grace@example.com", "hopper@example.com"]
        s.ready = ["alan@example.com"]
        s.connect.state = "failed"
        s.connect.message = "Google says the permission was declined. Run this again and choose Allow on the Google screen."
        s.connect.tail = ["Error 403: access_denied"]
        s.connectTimeoutSec = 600
        return s
    }

    static var intakeStatus: PasteIntakeStatus {
        var s = PasteIntakeStatus()
        s.llm = false
        s.settings = ["NC_INTAKE_PASTE_MAX_CHARS": 20000, "NC_INTAKE_MAX_PEOPLE": 60, "NC_INTAKE_WEB": "0"]
        s.writes = ["name", "company", "position", "how_we_met", "linkedin_url"]
        s.matchKeys = ["name_norm", "email", "phone_last9", "linkedin_slug"]
        s.sentences = [
            "Paste names, LinkedIn addresses, emails and phone numbers. One person per line.",
            "No API key is set, so the built-in parser reads the paste. This works fully offline.",
            "Web lookup is off, so no link you paste is opened.",
            "Nothing leaves this machine.",
            "Nothing is saved until you review the list and confirm it.",
        ]
        return s
    }

    static var draft: PasteDraftOffer {
        var d = PasteDraft()
        d.text = "Selftest Ada Lovelace\nSelftest Alan Turing\nSelftest Grace Hopper"
        d.proposals = ["p1", "p2", "p3"].map { ref in
            var p = PasteProposal()
            p.ref = ref
            p.verdict = "new"
            return p
        }
        return PasteDraftOffer(draft: d, raw: .object([:]))
    }

    static var emptyPlan: PastePlan {
        PastePlan(proposals: [], report: PasteReport(), allowedLookupUrls: [], rawProposals: .array([]), rawReport: .object([:]))
    }

    static func outcome(_ i: Int, _ name: String, id: Int?, wrote: [String] = [], kept: [String] = [], refused: String = "") -> PasteOutcome {
        var o = PasteOutcome()
        o.index = i
        o.name = name
        o.contactID = id
        o.wrote = wrote
        o.kept = kept
        o.refused = refused
        return o
    }

    static var receipt: PasteReceipt {
        var r = PasteReceipt()
        r.created = 2
        r.enriched = 1
        r.submitted = 4
        r.wroteAnything = true
        r.accounting.submitted = 4
        r.accounting.accounted = 4
        r.sentences = [
            "2 people added and 1 person updated.",
            "4 fields filled.",
            "1 field already had a value and was left alone. Intake fills empty fields only.",
            "2 email or phone numbers saved.",
            "1 person had an unconfirmed name and was not created.",
        ]
        r.outcomes = [
            outcome(1, "Selftest Ada Lovelace", id: 1, wrote: ["name", "position"]),
            outcome(2, "Selftest Alan Turing", id: 3, wrote: ["name", "identifier:email", "identifier:phone"]),
            outcome(3, "Grace Hopper", id: 2, wrote: ["company"], kept: ["position"]),
            outcome(4, "Selftest Grace Hopper", id: nil,
                    refused: "The name \"Selftest Grace Hopper\" was not confirmed, so nobody was created. A name taken from a LinkedIn address or an email is a guess until you say otherwise."),
        ]
        return r
    }
}

/// The paste review's content without the sheet around it, for the cards picture.
private struct PasteSnapshotCards: View {
    @AppEnvironment private var app
    @State private var store = PasteStore()

    var body: some View {
        PasteReviewPhase(store: store)
            .padding(Metrics.padView)
            .frame(width: 780, height: 1800, alignment: .topLeading)
        .background(Palette.panel)
        .task {
            await store.start(app: app, settle: false)
            await store.readSampleForSnapshot()
            app.markSettled()
        }
    }
}
