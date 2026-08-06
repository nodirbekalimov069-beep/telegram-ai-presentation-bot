import CoreGraphics
import Foundation

func click(_ point: CGPoint, button: CGMouseButton) {
    let down: CGEventType = button == .right ? .rightMouseDown : .leftMouseDown
    let up: CGEventType = button == .right ? .rightMouseUp : .leftMouseUp
    CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: point, mouseButton: button)?.post(tap: .cghidEventTap)
    usleep(100_000)
    CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: point, mouseButton: button)?.post(tap: .cghidEventTap)
}

click(CGPoint(x: 500, y: 744), button: .right)
usleep(900_000)
click(CGPoint(x: 590, y: 722), button: .left)
usleep(500_000)
