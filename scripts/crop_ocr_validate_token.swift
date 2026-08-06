import AppKit
import CoreGraphics
import Foundation
import Vision

func validate(_ token: String, expected: String) -> Bool {
    guard let url = URL(string: "https://api.telegram.org/bot\(token)/getMe") else { return false }
    let semaphore = DispatchSemaphore(value: 0)
    var valid = false
    URLSession.shared.dataTask(with: url) { data, _, _ in
        defer { semaphore.signal() }
        guard
            let data,
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            json["ok"] as? Bool == true,
            let result = json["result"] as? [String: Any],
            let username = result["username"] as? String
        else { return }
        valid = username.caseInsensitiveCompare(expected) == .orderedSame
    }.resume()
    _ = semaphore.wait(timeout: .now() + 10)
    return valid
}

func store(_ token: String, envPath: String) throws {
    var env = try String(contentsOfFile: envPath, encoding: .utf8)
    let regex = try NSRegularExpression(pattern: #"(?m)^TELEGRAM_BOT_TOKEN=.*$"#)
    let range = NSRange(env.startIndex..<env.endIndex, in: env)
    env = regex.stringByReplacingMatches(in: env, range: range, withTemplate: "TELEGRAM_BOT_TOKEN=" + token)
    try env.write(toFile: envPath, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envPath)
}

guard CommandLine.arguments.count == 4 else { exit(2) }
let sourcePath = CommandLine.arguments[1]
let envPath = CommandLine.arguments[2]
let expected = CommandLine.arguments[3]
guard
    let image = NSImage(contentsOfFile: sourcePath),
    let source = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
else { exit(3) }

let tokenPattern = try NSRegularExpression(pattern: #"[1-9][0-9]{5,14}:[A-Za-z0-9_-]{30,}"#)
let cropRects = [
    CGRect(x: 620, y: 1370, width: 800, height: 230),
    CGRect(x: 620, y: 300, width: 800, height: 230),
    CGRect(x: 600, y: 1320, width: 850, height: 320),
    CGRect(x: 600, y: 260, width: 850, height: 320),
]

for cropRect in cropRects {
    guard let crop = source.cropping(to: cropRect) else { continue }
    let scale = 3
    let width = crop.width * scale
    let height = crop.height * scale
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { continue }
    context.interpolationQuality = .high
    context.draw(crop, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let enlarged = context.makeImage() else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: enlarged, options: [:])
    try handler.perform([request])
    let observations = (request.results ?? []).sorted {
        if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.01 {
            return $0.boundingBox.midY > $1.boundingBox.midY
        }
        return $0.boundingBox.midX < $1.boundingBox.midX
    }

    var beams = [""]
    for observation in observations {
        let choices = observation.topCandidates(10).map(\.string)
        var next = [String]()
        for beam in beams {
            next.append(beam)
            for choice in choices { next.append(beam + choice) }
        }
        beams = Array(next.prefix(5000))
    }

    var tested = Set<String>()
    for beam in beams {
        let compact = beam.replacingOccurrences(of: "\\s+", with: "", options: .regularExpression)
        let range = NSRange(compact.startIndex..<compact.endIndex, in: compact)
        for match in tokenPattern.matches(in: compact, range: range) {
            guard let tokenRange = Range(match.range, in: compact) else { continue }
            let token = String(compact[tokenRange])
            guard tested.insert(token).inserted else { continue }
            if validate(token, expected: expected) {
                try store(token, envPath: envPath)
                print("CROPPED_OCR_TOKEN_VALIDATED_AND_STORED")
                exit(0)
            }
        }
    }
}

FileHandle.standardError.write(Data("validated token not found in crops\n".utf8))
exit(1)
