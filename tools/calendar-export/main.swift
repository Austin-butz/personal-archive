// CalendarExport: print every calendar event occurrence in a date range as JSON Lines.
//
// Usage: CalendarExport <start-unix-seconds> <end-unix-seconds>
//
// EventKit expands recurring events (including moved and deleted occurrences),
// so each line is one real occurrence. The last line is {"done": true, "count": N};
// a missing sentinel means the export failed. Launch via `open` (see build.sh)
// so macOS attributes the Calendar permission to this app, not the parent process.
import EventKit
import Foundation

func fail(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

let args = CommandLine.arguments
guard args.count == 3, let s = Double(args[1]), let e = Double(args[2]), s < e else {
    fail("usage: CalendarExport <start-unix-seconds> <end-unix-seconds>", 64)
}

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestFullAccessToEvents { ok, _ in
    granted = ok
    sem.signal()
}
sem.wait()
guard granted else {
    fail("calendar access denied: allow CalendarExport in System Settings > Privacy & Security > Calendars", 77)
}

let utc = ISO8601DateFormatter()
utc.formatOptions = [.withInternetDateTime]
utc.timeZone = TimeZone(identifier: "UTC")

// All-day events are floating dates; keep them as YYYY-MM-DD in the local calendar.
let day = DateFormatter()
day.locale = Locale(identifier: "en_US_POSIX")
day.dateFormat = "yyyy-MM-dd"
day.timeZone = TimeZone.current

let out = FileHandle.standardOutput
func emit(_ obj: [String: Any]) {
    var data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    data.append(0x0A)
    out.write(data)
}

let calendars = store.calendars(for: .event)
let end = Date(timeIntervalSince1970: e)
var cursor = Date(timeIntervalSince1970: s)
let maxSpan: TimeInterval = 3 * 365 * 86400  // EventKit caps a predicate at 4 years
var seen = Set<String>()  // events spanning a chunk boundary are returned twice

while cursor < end {
    let chunkEnd = min(cursor.addingTimeInterval(maxSpan), end)
    let pred = store.predicateForEvents(withStart: cursor, end: chunkEnd, calendars: calendars)
    for ev in store.events(matching: pred) {
        let uid = ev.calendarItemExternalIdentifier ?? ev.calendarItemIdentifier
        let fmt: (Date) -> String = ev.isAllDay ? { day.string(from: $0) } : { utc.string(from: $0) }
        // occurrenceDate is the *original* slot, so a moved occurrence keeps its identity.
        let occurrence = fmt(ev.occurrenceDate ?? ev.startDate)
        let key = "\(uid)/\(occurrence)"
        if !seen.insert(key).inserted { continue }
        var obj: [String: Any] = [
            "uid": uid,
            "occurrence": occurrence,
            "calendar": ev.calendar.title,
            "title": ev.title ?? "",
            "start": fmt(ev.startDate),
            "end": fmt(ev.endDate),
            "all_day": ev.isAllDay,
        ]
        if let notes = ev.notes, !notes.isEmpty { obj["notes"] = notes }
        if let loc = ev.location, !loc.isEmpty { obj["location"] = loc }
        emit(obj)
    }
    cursor = chunkEnd
}
emit(["done": true, "count": seen.count])
