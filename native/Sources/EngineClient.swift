import Foundation
import os

// The ONE place that knows the engine is reached over HTTP on 127.0.0.1. Screens call
// get/post/put/delete with a path and get Models back; milestone 3 swaps this transport for
// a private connection without touching a screen.
//
// What the engine demands of a client (contract §1), and how this file meets it:
// - Host must carry the exact port: we always use http://127.0.0.1:PORT (never "localhost",
//   which resolves to ::1 first while the engine listens on IPv4 only).
// - No Origin, no Sec-Fetch-Site: URLSession sends neither, and we never add them.
// - Bodies go as `httpBody` with Content-Type application/json, never as a stream: the engine
//   does not decode chunked bodies and would read `{}` and silently do the wrong thing.
// - A write the engine rejects before reading its body leaves the body on the socket, and
//   the NEXT request on that connection comes back as an HTML 400/501. We retry that once.
// - Error bodies are decoded only when they say they are JSON.

/// Something went wrong talking to the engine, in words a person can read.
///
/// NC-363. `errorDescription` is the ONLY text of an engine failure that reaches a
/// screen, so it never names a route, an HTTP status, a port or a coding path, and it
/// never repeats an exception's text. The cases keep that detail for the log
/// (`technicalDetail`, written by `EngineClient`). Two kinds of engine sentence are shown
/// as the engine wrote them: a refusal (anything below 500, which the engine words for
/// people on purpose), and a 502 or 504 (the engine saying why something outside it
/// failed: a calendar address that stopped working, a helper that timed out), unless it
/// carries developer text. A 500 may be an exception's text, so it is replaced.
enum EngineError: LocalizedError, Sendable, Equatable {
    /// Nothing answered: the engine is not running, or not on this port.
    case unreachable(port: Int)
    case timedOut(seconds: Int)
    /// The engine refused and said why (its `{"error": "..."}` sentence).
    case engine(status: Int, message: String)
    /// The engine answered with something that is not its JSON (an HTML error page).
    case unexpectedAnswer(status: Int)
    /// The answer's shape differs from the contract. `path` is the coding path, never values.
    case decoding(route: String, path: String, problem: String)

    /// The web page's own sentences for the same cases (static/app/api.js `SAY_WENT_WRONG`).
    static let wentWrong = "Something went wrong inside Network CRM."
    private static let reopenAdvice = "Try again, and if it keeps happening, quit and reopen the app."

    /// What happened, as one short sentence.
    var problem: String {
        switch self {
        case .unreachable:
            "Network CRM is not answering."
        case .timedOut(let seconds):
            "Network CRM did not answer within \(Self.span(seconds))."
        case .engine(let status, let message):
            Self.engineSentence(status: status, message: message) ?? (status >= 500 ? Self.wentWrong : "Network CRM could not do that.")
        case .unexpectedAnswer, .decoding:
            Self.wentWrong
        }
    }

    /// What to do about it. Empty when the engine's own sentence already says, or when
    /// asking again would only be refused again.
    var advice: String {
        switch self {
        case .unreachable:
            "Quit and reopen the app, then try again."
        case .timedOut:
            "Try again."
        case .engine(let status, let message):
            Self.engineSentence(status: status, message: message) != nil || status < 500
                ? "" : Self.reopenAdvice
        case .unexpectedAnswer, .decoding:
            Self.reopenAdvice
        }
    }

    var errorDescription: String? { [problem, advice].filter { !$0.isEmpty }.joined(separator: " ") }

    /// The HTTP status, when the engine answered at all (404 = unknown id, 400 = bad input).
    var status: Int? {
        switch self {
        case .engine(let s, _), .unexpectedAnswer(let s): s
        default: nil
        }
    }

    /// The engine's sentence when it is meant for a person, else nil.
    private static func engineSentence(status: Int, message: String) -> String? {
        guard !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        if status < 500 { return message }
        if (status == 502 || status == 504), !carriesDeveloperText(message) { return message }
        return nil
    }

    /// The same list as static/app/api.js `DEVELOPER_TEXT`.
    private static func carriesDeveloperText(_ s: String) -> Bool {
        s.range(
            of: #"/api/|\bHTTP\b|Internal Server Error|Traceback|Error code|Exception\b"#,
            options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// "30 seconds", "5 minutes".
    private static func span(_ seconds: Int) -> String {
        let s = max(1, seconds)
        return s < 120 ? "\(s) \(s == 1 ? "second" : "seconds")" : "\(Int((Double(s) / 60).rounded())) minutes"
    }

    /// Everything the person is not shown: route, status, port, coding path, and what the
    /// engine said. For the log only. The engine's message may hold a name or a value.
    var technicalDetail: String {
        switch self {
        case .unreachable(let port):
            port == 0 ? "nothing answered on the private connection" : "nothing answered on port \(port)"
        case .timedOut(let seconds): "no answer within \(seconds) s"
        case .engine(let status, _): "the engine answered \(status)"
        case .unexpectedAnswer(let status): "the answer was not the engine's JSON (status \(status))"
        case .decoding(let route, let path, let problem):
            "the answer to \(route) did not match the contract at \(path.isEmpty ? "the top" : path): \(problem)"
        }
    }

    /// One sentence for ANY error about to be drawn: an `EngineError`'s own, a sentence the
    /// app wrote itself (a `LocalizedError`), or the file and network messages macOS words
    /// for people. Anything else is a Swift type name and a code, so it becomes the
    /// plain sentence and the log keeps what it was.
    static func sentence(for error: Error) -> String {
        if let e = error as? EngineError { return e.errorDescription ?? wentWrong }
        if let e = error as? LocalizedError, let d = e.errorDescription, !d.isEmpty { return d }
        let ns = error as NSError
        if [NSCocoaErrorDomain, NSURLErrorDomain, NSPOSIXErrorDomain].contains(ns.domain), !ns.localizedDescription.isEmpty {
            return ns.localizedDescription
        }
        EngineClient.log.error("an error with no sentence of its own reached a screen: \(String(describing: error), privacy: .private)")
        return wentWrong + " " + reopenAdvice
    }
}

/// What to send to `POST /api/chat`.
struct AskRequest: Sendable {
    var message: String
    /// nil = the newest chat (or a first one is created). Never send 0: it 404s.
    var conversationID: Int?
    var shape: Shape?
    /// Lets a second request stop this one. Must match /^[A-Za-z0-9_-]{8,64}$/.
    var runID: String = UUID().uuidString

    init(message: String, conversationID: Int? = nil, shape: Shape? = nil) {
        self.message = message
        self.conversationID = conversationID
        self.shape = shape
    }
}

final class EngineClient: Sendable {
    /// Ordinary reads and writes. The web page uses 20 s; 30 leaves room on a busy Mac.
    static let defaultTimeout: TimeInterval = 30
    /// A long Ask answer legitimately takes minutes (`ask.js:96`).
    static let askTimeout: TimeInterval = 180
    /// Imports (milestone 2): the engine's own import cap is 300 s.
    static let importTimeout: TimeInterval = 315

    /// Where the engine is: the app's private socket file, or (development) a port.
    let address: EngineAddress
    /// The port in development, 0 on the private connection.
    var port: Int { address.port }
    /// Requests are built against this; on the private connection only its path and
    /// query are sent.
    let baseURL: URL
    private let session: URLSession

    /// Where the detail a person is not shown goes (NC-363): Console.app, subsystem
    /// io.github.william-agehouse.networkcrm.native, category "engine". Routes, statuses
    /// and coding paths are public; what the engine said is private, because it may hold a
    /// name.
    static let log = Logger(subsystem: "io.github.william-agehouse.networkcrm.native", category: "engine")

    /// Writes an engine failure's detail to the log and hands it back to be thrown. A
    /// refusal (below 500) is the engine working as designed, so it is only noted.
    private func noted(_ e: EngineError, _ route: String) -> EngineError {
        let routeOnly = route.split(separator: "?").first.map(String.init) ?? route
        var message = ""
        if case .engine(_, let m) = e { message = m }
        if let s = e.status, s < 500, s != 0 {
            Self.log.info("\(routeOnly, privacy: .public): \(e.technicalDetail, privacy: .public) \(message, privacy: .private)")
        } else {
            Self.log.error("\(routeOnly, privacy: .public): \(e.technicalDetail, privacy: .public) \(message, privacy: .private)")
        }
        return e
    }

    convenience init(port: Int) {
        self.init(address: .port(port))
    }

    init(address: EngineAddress) {
        self.address = address
        switch address {
        case .port(let port): self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
        case .socket: self.baseURL = URL(string: "http://localhost")!
        }
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.timeoutIntervalForRequest = Self.defaultTimeout
        // The per-request timeoutInterval is what bounds each call; this only has to be
        // longer than the longest of them.
        config.timeoutIntervalForResource = Self.importTimeout + 60
        config.httpAdditionalHeaders = ["Accept": "application/json"]
        // Ask and Stop must run side by side on separate connections.
        config.httpMaximumConnectionsPerHost = 6
        self.session = URLSession(configuration: config)
    }

    // MARK: Typed calls

    func get<T: Decodable>(
        _ path: String, query: [String: String] = [:], as type: T.Type = T.self,
        timeout: TimeInterval = EngineClient.defaultTimeout
    ) async throws -> T {
        let (data, http) = try await send("GET", path, query: query, body: nil, timeout: timeout)
        return try decode(data, http, route: path)
    }

    func post<T: Decodable, Body: Encodable & Sendable>(
        _ path: String, body: Body, as type: T.Type = T.self,
        timeout: TimeInterval = EngineClient.defaultTimeout
    ) async throws -> T {
        let (data, http) = try await send("POST", path, query: [:], body: try JSONEncoder().encode(body), timeout: timeout)
        return try decode(data, http, route: path)
    }

    /// A POST with an empty object as its body (`reconnectskip`, `purge`, `brief`…).
    func post<T: Decodable>(
        _ path: String, as type: T.Type = T.self, timeout: TimeInterval = EngineClient.defaultTimeout
    ) async throws -> T {
        try await post(path, body: [String: JSONValue](), as: type, timeout: timeout)
    }

    func put<T: Decodable, Body: Encodable & Sendable>(
        _ path: String, body: Body, as type: T.Type = T.self,
        timeout: TimeInterval = EngineClient.defaultTimeout
    ) async throws -> T {
        let (data, http) = try await send("PUT", path, query: [:], body: try JSONEncoder().encode(body), timeout: timeout)
        return try decode(data, http, route: path)
    }

    /// DELETE never carries a body: one the engine does not read poisons the connection.
    func delete<T: Decodable>(
        _ path: String, as type: T.Type = T.self, timeout: TimeInterval = EngineClient.defaultTimeout
    ) async throws -> T {
        let (data, http) = try await send("DELETE", path, query: [:], body: nil, timeout: timeout)
        return try decode(data, http, route: path)
    }

    // MARK: Photos

    /// The engine's `photo` field is RELATIVE ("/api/photo/12?v=1712345678"); this makes it
    /// absolute. Returns nil for "".
    func absoluteURL(_ relative: String) -> URL? {
        guard !relative.isEmpty else { return nil }
        return URL(string: relative, relativeTo: baseURL)?.absoluteURL
    }

    /// The photo route for a person. `version` is the `v` from their `photo` field; after
    /// your own upload pass a fresh nonce instead, because two uploads inside one second
    /// get the same `v` and the engine marks photos immutable for a week.
    func photoURL(id: Int, version: String? = nil) -> URL {
        var c = URLComponents(url: baseURL.appending(path: "api/photo/\(id)"), resolvingAgainstBaseURL: false)!
        if let version, !version.isEmpty { c.queryItems = [URLQueryItem(name: "v", value: version)] }
        return c.url!
    }

    /// Image bytes for a `photo` path (or nil when the person has none: the engine 404s).
    func photoData(_ relative: String) async throws -> Data? {
        guard let url = absoluteURL(relative) else { return nil }
        var req = URLRequest(url: url, cachePolicy: .returnCacheDataElseLoad, timeoutInterval: Self.defaultTimeout)
        req.httpMethod = "GET"
        let (data, resp) = try await perform(req, method: "GET")
        guard let http = resp as? HTTPURLResponse else {
            throw noted(EngineError.unexpectedAnswer(status: 0), relative)
        }
        if http.statusCode == 404 { return nil }
        guard (200..<300).contains(http.statusCode) else {
            throw noted(Self.error(from: data, http), relative)
        }
        return data
    }

    // MARK: Ask

    /// Sends a question and waits for the answer (up to 3 minutes).
    ///
    /// Stopping: cancel the Task that awaits this call, or call `stopAsk(runID:)` yourself.
    /// Either way this call still RETURNS the engine's answer (`stopped == true`, the people
    /// found so far and the real bill): Stop is a second request with the same run id while
    /// the first stays open, exactly as the web page does it (`ask.js:813-824`). The
    /// question request itself is never torn down.
    func ask(_ request: AskRequest) async throws -> ChatAnswer {
        var body: [String: JSONValue] = ["message": .string(request.message), "run_id": .string(request.runID)]
        if let id = request.conversationID { body["conversation_id"] = .int(id) }
        if let s = request.shape {
            body["shape"] = ["people": .string(s.people), "detail": .string(s.detail), "search": .string(s.search)]
        }
        // Unstructured on purpose: cancelling the caller must not cancel this request.
        let work = Task.detached { [self] in
            try await self.post("/api/chat", body: body, as: ChatAnswer.self, timeout: Self.askTimeout)
        }
        let runID = request.runID
        return try await withTaskCancellationHandler {
            try await work.value
        } onCancel: {
            Task.detached { [self] in _ = try? await self.stopAsk(runID: runID) }
        }
    }

    /// Asks the engine to stop a running answer. "unknown" is normal: it already finished.
    @discardableResult
    func stopAsk(runID: String) async throws -> ChatStopResult {
        try await post("/api/chat/stop", body: ["run_id": JSONValue.string(runID)], as: ChatStopResult.self)
    }

    // MARK: Transport

    private func url(_ path: String, query: [String: String]) -> URL {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        // A path may carry its own query ("/api/contacts?sort=closeness").
        let parts = path.split(separator: "?", maxSplits: 1).map(String.init)
        c.path = parts.first ?? path
        var items = parts.count > 1 ? (URLComponents(string: "?" + parts[1])?.queryItems ?? []) : []
        items += query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        if !items.isEmpty {
            c.queryItems = items
            // URLComponents leaves "+" alone, and the engine's parse_qs reads "+" as a space.
            c.percentEncodedQuery = c.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        }
        return c.url!
    }

    private func send(
        _ method: String, _ path: String, query: [String: String], body: Data?, timeout: TimeInterval
    ) async throws -> (Data, HTTPURLResponse) {
        var req = URLRequest(url: url(path, query: query), cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.httpMethod = method
        if method == "POST" || method == "PUT" {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body ?? Data("{}".utf8)
        }
        let (data, resp) = try await perform(req, method: method)
        guard let http = resp as? HTTPURLResponse else {
            throw noted(EngineError.unexpectedAnswer(status: 0), path)
        }
        return (data, http)
    }

    /// One request, retried once when the connection had been poisoned by an earlier
    /// rejected write (an HTML 400/501), or for a read whose kept-alive connection dropped.
    private func perform(_ req: URLRequest, method: String) async throws -> (Data, URLResponse) {
        var attempt = 0
        while true {
            attempt += 1
            do {
                let (data, resp) = try await exchange(req, on: session)
                if attempt == 1, let http = resp as? HTTPURLResponse,
                    http.statusCode == 400 || http.statusCode == 501, !Self.isJSON(http)
                {
                    continue
                }
                return (data, resp)
            } catch let e as URLError {
                switch e.code {
                case .cancelled:
                    throw CancellationError()
                case .timedOut:
                    throw noted(EngineError.timedOut(seconds: Int(req.timeoutInterval)), req.url?.path ?? "")
                case .networkConnectionLost where attempt == 1 && method == "GET":
                    continue
                default:
                    throw noted(EngineError.unreachable(port: port), req.url?.path ?? "")
                }
            }
        }
    }

    /// One request on whichever connection this client has: URLSession for a port, the
    /// private socket file otherwise (NC-330). Both fail with the same URLError codes.
    func exchange(_ req: URLRequest, on session: URLSession) async throws -> (Data, URLResponse) {
        switch address {
        case .port: return try await session.data(for: req)
        case .socket(let path): return try await PrivateConnection.data(for: req, socketPath: path)
        }
    }

    private func decode<T: Decodable>(_ data: Data, _ http: HTTPURLResponse, route: String) throws -> T {
        guard (200..<300).contains(http.statusCode) else {
            throw noted(Self.error(from: data, http), route)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch let e as DecodingError {
            let (path, problem) = Self.describe(e)
            let routeOnly = route.split(separator: "?").first.map(String.init) ?? route
            throw noted(EngineError.decoding(route: routeOnly, path: path, problem: problem), route)
        }
    }

    private static func isJSON(_ http: HTTPURLResponse) -> Bool {
        (http.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased().contains("application/json")
    }

    private struct ErrorBody: Decodable { let error: String? }

    private static func error(from data: Data, _ http: HTTPURLResponse) -> EngineError {
        if isJSON(http), let body = try? JSONDecoder().decode(ErrorBody.self, from: data), let msg = body.error {
            return .engine(status: http.statusCode, message: msg)
        }
        return .unexpectedAnswer(status: http.statusCode)
    }

    /// Coding path and kind of problem only — never the offending value, so the self-test
    /// can print it without leaking anyone's details.
    static func describe(_ e: DecodingError) -> (path: String, problem: String) {
        func render(_ path: [CodingKey]) -> String {
            path.map { $0.intValue.map { "[\($0)]" } ?? ".\($0.stringValue)" }.joined()
                .trimmingCharacters(in: CharacterSet(charactersIn: "."))
        }
        switch e {
        case .typeMismatch(let t, let ctx): return (render(ctx.codingPath), "expected \(t)")
        case .valueNotFound(let t, let ctx): return (render(ctx.codingPath), "missing \(t)")
        case .keyNotFound(let k, let ctx): return (render(ctx.codingPath + [k]), "key not found")
        case .dataCorrupted(let ctx): return (render(ctx.codingPath), "not valid JSON")
        @unknown default: return ("", "unknown decoding error")
        }
    }
}
