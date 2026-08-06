import AppKit
import Foundation
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func compact(_ value: String) -> String {
    value.replacingOccurrences(of: "\\s+", with: "", options: .regularExpression)
}

func telegramUsername(for token: String) -> String? {
    guard let url = URL(string: "https://api.telegram.org/bot\(token)/getMe") else { return nil }
    let semaphore = DispatchSemaphore(value: 0)
    var username: String?
    URLSession.shared.dataTask(with: url) { data, _, _ in
        defer { semaphore.signal() }
        guard
            let data,
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            object["ok"] as? Bool == true,
            let result = object["result"] as? [String: Any]
        else { return }
        username = result["username"] as? String
    }.resume()
    _ = semaphore.wait(timeout: .now() + 15)
    return username
}

guard CommandLine.arguments.count == 4 else {
    fail("usage: script <screenshot> <env-file> <expected-username>")
}
let screenshotPath = CommandLine.arguments[1]
let envPath = CommandLine.arguments[2]
let expectedUsername = CommandLine.arguments[3].trimmingCharacters(in: CharacterSet(charactersIn: "@"))

guard
    let image = NSImage(contentsOfFile: screenshotPath),
    let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
else { fail("screenshot could not be loaded") }

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try handler.perform([request])
let observations = (request.results ?? []).sorted {
    if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.008 {
        return $0.boundingBox.midY > $1.boundingBox.midY
    }
    return $0.boundingBox.midX < $1.boundingBox.midX
}

let tokenRegex = try NSRegularExpression(pattern: #"[1-9][0-9]{5,14}:[A-Za-z0-9_-]{30,}"#)
var tested = Set<String>()
var validToken: String?

outer: for index in observations.indices {
    let firstCandidates = observations[index].topCandidates(10).map(\.string)
    let secondCandidates = index + 1 < observations.count
        ? observations[index + 1].topCandidates(10).map(\.string)
        : [""]
    let thirdCandidates = index + 2 < observations.count
        ? observations[index + 2].topCandidates(5).map(\.string)
        : [""]

    for first in firstCandidates {
        for second in [""] + secondCandidates {
            for third in [""] + thirdCandidates {
                let candidateText = compact(first + second + third)
                let range = NSRange(candidateText.startIndex..<candidateText.endIndex, in: candidateText)
                for match in tokenRegex.matches(in: candidateText, range: range) {
                    guard let swiftRange = Range(match.range, in: candidateText) else { continue }
                    let token = String(candidateText[swiftRange])
                    guard tested.insert(token).inserted else { continue }
                    if telegramUsername(for: token)?.caseInsensitiveCompare(expectedUsername) == .orderedSame {
                        validToken = token
                        break outer
                    }
                }
            }
        }
    }
}

guard let validToken else { fail("no validated token found for expected bot") }
guard var env = try? String(contentsOfFile: envPath, encoding: .utf8) else {
    fail(".env could not be loaded")
}
let envRegex = try NSRegularExpression(pattern: #"(?m)^TELEGRAM_BOT_TOKEN=.*$"#)
let envRange = NSRange(env.startIndex..<env.endIndex, in: env)
env = envRegex.stringByReplacingMatches(
    in: env,
    range: envRange,
    withTemplate: "TELEGRAM_BOT_TOKEN=" + validToken
)
try env.write(toFile: envPath, atomically: true, encoding: .utf8)
try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envPath)
print("VALIDATED_TOKEN_STORED")
