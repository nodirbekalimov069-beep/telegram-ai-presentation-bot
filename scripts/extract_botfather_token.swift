import AppKit
import Foundation
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: extract_botfather_token.swift <screenshot> <env-file>")
}
let screenshotPath = CommandLine.arguments[1]
let envPath = CommandLine.arguments[2]

guard
    let image = NSImage(contentsOfFile: screenshotPath),
    let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
else {
    fail("screenshot could not be loaded")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try handler.perform([request])

let observations = (request.results ?? []).sorted {
    if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.01 {
        return $0.boundingBox.midY > $1.boundingBox.midY
    }
    return $0.boundingBox.midX < $1.boundingBox.midX
}
let text = observations.compactMap { $0.topCandidates(1).first?.string }.joined()
let compact = text.replacingOccurrences(of: "\\s+", with: "", options: .regularExpression)
let pattern = #"[1-9][0-9]{5,14}:[A-Za-z0-9_-]{30,}"#
let regex = try NSRegularExpression(pattern: pattern)
let range = NSRange(compact.startIndex..<compact.endIndex, in: compact)
let matches = regex.matches(in: compact, range: range)
guard let match = matches.last, let tokenRange = Range(match.range, in: compact) else {
    fail("no Telegram bot token recognized")
}
let token = String(compact[tokenRange])

guard var env = try? String(contentsOfFile: envPath, encoding: .utf8) else {
    fail(".env could not be loaded")
}
let envRegex = try NSRegularExpression(pattern: #"(?m)^TELEGRAM_BOT_TOKEN=.*$"#)
let envRange = NSRange(env.startIndex..<env.endIndex, in: env)
env = envRegex.stringByReplacingMatches(
    in: env,
    range: envRange,
    withTemplate: "TELEGRAM_BOT_TOKEN=" + token
)
try env.write(toFile: envPath, atomically: true, encoding: .utf8)
try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envPath)
print("TOKEN_STORED_SECURELY")
