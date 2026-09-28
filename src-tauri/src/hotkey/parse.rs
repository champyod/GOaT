use tauri_plugin_global_shortcut::Code;

fn letter_code(c: char) -> Option<Code> {
    Some(match c.to_ascii_lowercase() {
        'a' => Code::KeyA,
        'b' => Code::KeyB,
        'c' => Code::KeyC,
        'd' => Code::KeyD,
        'e' => Code::KeyE,
        'f' => Code::KeyF,
        'g' => Code::KeyG,
        'h' => Code::KeyH,
        'i' => Code::KeyI,
        'j' => Code::KeyJ,
        'k' => Code::KeyK,
        'l' => Code::KeyL,
        'm' => Code::KeyM,
        'n' => Code::KeyN,
        'o' => Code::KeyO,
        'p' => Code::KeyP,
        'q' => Code::KeyQ,
        'r' => Code::KeyR,
        's' => Code::KeyS,
        't' => Code::KeyT,
        'u' => Code::KeyU,
        'v' => Code::KeyV,
        'w' => Code::KeyW,
        'x' => Code::KeyX,
        'y' => Code::KeyY,
        'z' => Code::KeyZ,
        _ => return None,
    })
}

fn digit_code(c: char) -> Option<Code> {
    Some(match c {
        '0' => Code::Digit0,
        '1' => Code::Digit1,
        '2' => Code::Digit2,
        '3' => Code::Digit3,
        '4' => Code::Digit4,
        '5' => Code::Digit5,
        '6' => Code::Digit6,
        '7' => Code::Digit7,
        '8' => Code::Digit8,
        '9' => Code::Digit9,
        _ => return None,
    })
}

fn named_code(lower: &str) -> Option<Code> {
    Some(match lower {
        "space" => Code::Space,
        "enter" => Code::Enter,
        "tab" => Code::Tab,
        "escape" | "esc" => Code::Escape,
        "backspace" => Code::Backspace,
        "delete" => Code::Delete,
        "up" => Code::ArrowUp,
        "down" => Code::ArrowDown,
        "left" => Code::ArrowLeft,
        "right" => Code::ArrowRight,
        "f1" => Code::F1,
        "f2" => Code::F2,
        "f3" => Code::F3,
        "f4" => Code::F4,
        "f5" => Code::F5,
        "f6" => Code::F6,
        "f7" => Code::F7,
        "f8" => Code::F8,
        "f9" => Code::F9,
        "f10" => Code::F10,
        "f11" => Code::F11,
        "f12" => Code::F12,
        _ => return None,
    })
}

fn parse_key_code(lower: &str) -> Option<Code> {
    let mut chars = lower.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) if c.is_ascii_alphabetic() => letter_code(c),
        (Some(c), None) if c.is_ascii_digit() => digit_code(c),
        _ => named_code(lower),
    }
}

pub fn parse_shortcut(s: &str) -> Option<tauri_plugin_global_shortcut::Shortcut> {
    use tauri_plugin_global_shortcut::{Modifiers, Shortcut};
    let mut mods = Modifiers::empty();
    let mut key: Option<Code> = None;
    for part in s.split('+').map(str::trim).filter(|p| !p.is_empty()) {
        let lower = part.to_ascii_lowercase();
        match lower.as_str() {
            "ctrl" | "control" => mods |= Modifiers::CONTROL,
            "shift" => mods |= Modifiers::SHIFT,
            "alt" => mods |= Modifiers::ALT,
            "super" | "win" | "meta" | "cmd" | "command" => mods |= Modifiers::SUPER,
            _ => {
                if key.is_some() {
                    return None;
                }
                key = Some(parse_key_code(&lower)?);
            }
        }
    }
    Some(Shortcut::new(
        if mods.is_empty() { None } else { Some(mods) },
        key?,
    ))
}
