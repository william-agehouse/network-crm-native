import SwiftUI

// "Coming up": the next few personal dates across the whole network — birthdays,
// anniversaries, marked days, dated lists — on the page you are on anyway (NC-040,
// static/app/people.js:546-600). At rest it costs nothing: with nothing coming up, or when
// the engine cannot answer, it is not drawn at all. The engine decides what counts and how
// far ahead to look (14 days, never calendar meetings); this only draws.

struct PeopleComingUpStrip: View {
    let upcoming: UpcomingResponse
    @AppEnvironment private var app

    var body: some View {
        HStack(spacing: 6) {
            Text("Coming up")
                .font(.system(size: Metrics.fsOverline, weight: .semibold))
                .foregroundStyle(Palette.muted)
                .padding(.trailing, 2)
                .fixedSize()
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(upcoming.shown) { item in chip(item) }
                    if upcoming.more > 0 {
                        // people.js:588-589
                        Text(verbatim: "+\(upcoming.more) more in \(upcoming.days) days")
                            .font(.system(size: Metrics.fsOverline))
                            .foregroundStyle(Palette.muted)
                            .fixedSize()
                    }
                }
                .padding(.vertical, 1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Palette.panel)
        .overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Coming up")
    }

    /// Every chip goes somewhere: a date opens the person, a dated list opens the list. One
    /// with neither is plain text rather than a button that does nothing (NC-006).
    @ViewBuilder private func chip(_ item: UpcomingItem) -> some View {
        let content = HStack(spacing: 5) {
            Image(systemName: Display.dateKindSymbol(item.kind))
                .foregroundStyle(Palette.muted)
            Text(verbatim: item.title)
                .foregroundStyle(Palette.ink)
                .lineLimit(1)
            Text(verbatim: Display.whenShort(item.daysUntil))
                .foregroundStyle(item.daysUntil <= 1 ? Display.Staleness.fresh.color : Palette.muted)
        }
        .font(.system(size: Metrics.fsOverline))
        .padding(.horizontal, 8)
        .frame(height: 22)
        .background(Capsule().fill(Palette.panel2))
        .overlay(Capsule().strokeBorder(Palette.line))
        .fixedSize()

        if let contact = item.contactID {
            Button { app.selectedPersonID = contact } label: { content }
                .buttonStyle(.plain)
                .help(item.reason)
                .accessibilityLabel("\(item.title), \(item.reason)")
        } else if let list = item.listID {
            Button { app.openList(list) } label: { content }
                .buttonStyle(.plain)
                .help(item.reason)
                .accessibilityLabel("\(item.title), \(item.reason)")
        } else {
            content.help(item.reason)
        }
    }
}
