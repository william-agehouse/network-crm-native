import Foundation
import Observation

// Small requests between the Data page, the Settings page, the merge sheet and the menu
// commands, which AppModel (a shared file) has no property for: which Settings row to open
// when the Data page sends you there (the web page's #settings?row=identity), and a line to
// show once a merge or an export started elsewhere has finished (the web page's toast).

@MainActor
@Observable
final class DataSettingsBus {
    static let shared = DataSettingsBus()

    /// A Settings row to open and bring into view, then cleared by the Settings page.
    var settingsRow: String?
    /// "Merged Ada into Ada Lovelace", shown by whichever of the two pages is open.
    private(set) var announcement: String?
    /// Bumped with every announcement, so the same words twice still show twice.
    private(set) var announceTick = 0

    func announce(_ line: String) {
        announcement = line
        announceTick += 1
    }
}
