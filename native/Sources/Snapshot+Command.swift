import SwiftUI

// Extra --snapshot scenes for first-run setup and the command bar. Owned by that screen's files; Snapshot.run adds
// these to its own list. `firstID` is the closest person, for scenes that need someone.
//
// The command bar is drawn from NC_NATIVE_PORT's engine. First-run setup is always drawn
// from the empty scratch engine on 8768, whatever NC_NATIVE_PORT says: its steps read the
// import sources, whose answer comes from files in ~/Library, and a picture of setup is a
// picture of a Mac with nothing imported yet.

extension Snapshot {
    static func commandScenes(firstID: Int?) -> [Scene] {
        let bar: @MainActor (AppModel) -> AnyView = { _ in AnyView(CommandBar()) }
        let barSize = NSSize(width: CommandBar.size.width, height: CommandBar.size.height)
        let setup: @MainActor (AppModel) -> AnyView = { _ in AnyView(OnboardingFlow()) }
        let setupSize = NSSize(width: OnboardingFlow.size.width, height: OnboardingFlow.size.height)
        let scratch = SelfTest.scratchPort

        var scenes: [Scene] = [
            // An empty box is the command list.
            Scene(name: "command-empty", view: bar, size: barSize) { $0.section = .people },
            // A name typed: people, conversations, then the two fallbacks.
            Scene(name: "command-name", view: bar, size: barSize) { $0.snapshotHint = "command:an" },
            // ">" narrows to commands; "> list" ranks the list commands first.
            Scene(name: "command-commands", view: bar, size: barSize) { $0.snapshotHint = "command:> list" },
        ]
        if let firstID {
            // With a person open, their own commands come first.
            scenes.append(Scene(name: "command-person", view: bar, size: barSize) { app in
                app.section = .people
                app.selectedPersonID = firstID
                app.snapshotHint = "command:>"
            })
        }
        for (name, hint) in [
            ("onboarding-hello", "onboarding"), ("onboarding-you", "onboarding-you"),
            ("onboarding-linkedin", "onboarding-linkedin"), ("onboarding-contacts", "onboarding-contacts"),
            ("onboarding-imessage", "onboarding-imessage"), ("onboarding-remember", "onboarding-remember"),
            ("onboarding-ai", "onboarding-ai"),
        ] {
            scenes.append(Scene(name: name, port: scratch, view: setup, size: setupSize) { $0.snapshotHint = hint })
        }
        return scenes
    }
}
