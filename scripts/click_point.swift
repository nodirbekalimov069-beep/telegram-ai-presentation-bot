import CoreGraphics
import Foundation

let args = CommandLine.arguments
if args.count != 4 { exit(2) }
let x = Double(args[1])!
let y = Double(args[2])!
let buttonName = args[3]
let point = CGPoint(x: x, y: y)
let button: CGMouseButton = buttonName == "right" ? .right : .left
let down: CGEventType = buttonName == "right" ? .rightMouseDown : .leftMouseDown
let up: CGEventType = buttonName == "right" ? .rightMouseUp : .leftMouseUp
CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: point, mouseButton: button)?.post(tap: .cghidEventTap)
usleep(80_000)
CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: point, mouseButton: button)?.post(tap: .cghidEventTap)
