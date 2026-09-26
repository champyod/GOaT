// Tesseract OCR sidecar for GOaT. Reads one image path from argv,
// prints recognized text (eng+tha, embedded tessdata) to stdout.
// Built as a separate binary so its dynamic-CRT C++ objects never
// link into the main app (which uses static CRT for ct2rs).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if let Err(e) = run() {
        eprintln!("tesseract-ocr sidecar error: {e:#}");
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    let path = std::env::args()
        .nth(1)
        .ok_or_else(|| anyhow::anyhow!("usage: tesseract-ocr <image.png>"))?;
    let api = load_api()?;
    ocr_with(&api, &path)
}

// Both languages are the point of this sidecar: Thai-only screens OCR to
// garbage without "tha", so a missing one means the binary was mis-built and
// must not degrade quietly.
const REQUIRED_LANGS: [&str; 2] = ["eng", "tha"];

// init_embedded() only accepts a single embedded key, so "eng+tha" can
// never match. Extract the embedded tessdata into a temp dir and init
// normally for true multi-language OCR.
fn load_api() -> anyhow::Result<tesseract_rs::TesseractAPI> {
    let embedded: Vec<(&str, &'static [u8])> = REQUIRED_LANGS
        .iter()
        .filter_map(|lang| tesseract_rs::get_embedded_tessdata(lang).map(|data| (*lang, data)))
        .collect();
    let found: Vec<&str> = embedded.iter().map(|(lang, _)| *lang).collect();
    let spec = init_spec(&found)?;
    let dir = std::env::temp_dir().join("goat-tessdata");
    std::fs::create_dir_all(&dir)
        .map_err(|e| anyhow::anyhow!("failed to create temp tessdata dir: {e}"))?;
    for (lang, data) in &embedded {
        std::fs::write(dir.join(format!("{lang}.traineddata")), data)
            .map_err(|e| anyhow::anyhow!("failed to stage {lang} tessdata: {e}"))?;
    }
    let dir_str = dir
        .to_str()
        .ok_or_else(|| anyhow::anyhow!("temp tessdata path not UTF-8"))?;
    let api = tesseract_rs::TesseractAPI::new();
    api.init(dir_str, &spec)
        .map_err(|e| anyhow::anyhow!("tesseract init failed: {e}"))?;
    // Sparse text: screenshots are scattered UI text, full page
    // segmentation is far slower for no accuracy gain here.
    api.set_variable("tessedit_pageseg_mode", "11")
        .map_err(|e| anyhow::anyhow!("tesseract set PSM failed: {e}"))?;
    Ok(api)
}

/// Resolve the tesseract init spec from the languages actually embedded in this
/// binary, failing loudly when any required one is missing. tesseract-rs only
/// embeds a language whose `.traineddata` was in its cache dir at build time and
/// merely warns otherwise, so a mis-built binary must be reported, not tolerated.
fn init_spec(available: &[&str]) -> anyhow::Result<String> {
    let missing: Vec<&str> = REQUIRED_LANGS
        .iter()
        .copied()
        .filter(|lang| !available.contains(lang))
        .collect();
    if !missing.is_empty() {
        return Err(anyhow::anyhow!(
            "binary has no embedded tessdata for {}: tesseract-rs silently skips \
             languages that are missing from its build-time cache. Rebuild with \
             sidecar-ocr/build.sh, which primes the cache before cargo runs.",
            missing.join(", ")
        ));
    }
    Ok(REQUIRED_LANGS.join("+"))
}

fn ocr_with(api: &tesseract_rs::TesseractAPI, path: &str) -> anyhow::Result<()> {
    let img = image::open(path)
        .map_err(|e| anyhow::anyhow!("failed to open {path}: {e}"))?
        .to_rgb8();
    let (width, height) = (img.width(), img.height());
    api.set_image(
        &img.into_raw(),
        width as i32,
        height as i32,
        3,
        3 * width as i32,
    )
    .map_err(|e| anyhow::anyhow!("tesseract set_image failed: {e}"))?;
    let text = api
        .get_utf8_text()
        .map_err(|e| anyhow::anyhow!("tesseract inference failed: {e}"))?;
    print!("{}", join_thai_spaces(text.trim()));
    Ok(())
}

// Tesseract inserts spaces between every Thai glyph, shredding words.
// Thai normally runs without inter-word spaces, so drop a space when it
// sits between two Thai-block characters. Spaces touching Latin, digits
// or punctuation are preserved.
fn is_thai(c: char) -> bool {
    ('\u{0E00}'..='\u{0E7F}').contains(&c)
}

fn join_thai_spaces(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    for (i, &c) in chars.iter().enumerate() {
        if c == ' ' {
            let prev = i.checked_sub(1).and_then(|j| chars.get(j));
            let next = chars.get(i + 1);
            if matches!(prev, Some(&p) if is_thai(p)) && matches!(next, Some(&n) if is_thai(n)) {
                continue;
            }
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_eng_present() {
        let langs = tesseract_rs::embedded_languages();
        assert!(
            langs.contains(&"eng"),
            "eng missing from embedded tessdata: {langs:?}"
        );
    }

    #[test]
    fn embedded_tha_present() {
        let langs = tesseract_rs::embedded_languages();
        assert!(
            langs.contains(&"tha"),
            "tha missing from embedded tessdata: {langs:?}"
        );
    }

    #[test]
    fn init_spec_needs_every_language() {
        assert_eq!(init_spec(&["eng", "tha"]).expect("both present"), "eng+tha");
    }

    #[test]
    fn init_spec_fails_loud_without_tha() {
        // The exact regression this guards: tesseract-rs embeds only what is in
        // its cache, so an eng-only binary used to degrade silently to English.
        let err = init_spec(&["eng"]).expect_err("tha missing must fail");
        let msg = err.to_string();
        assert!(msg.contains("tha"), "error must name the language: {msg}");
        assert!(msg.contains("build.sh"), "error must name the fix: {msg}");
    }

    #[test]
    fn init_spec_fails_loud_when_nothing_embedded() {
        let err = init_spec(&[]).expect_err("no tessdata must fail");
        let msg = err.to_string();
        assert!(msg.contains("eng"), "error must name the language: {msg}");
        assert!(msg.contains("tha"), "error must name the language: {msg}");
    }

    #[test]
    fn thai_spaces_join() {
        assert_eq!(join_thai_spaces("ไต ร ฟี"), "ไตรฟี");
        assert_eq!(join_thai_spaces("ส ิ พ 065"), "สิพ 065");
        assert_eq!(
            join_thai_spaces("tracking 100 จาก 100"),
            "tracking 100 จาก 100"
        );
        assert_eq!(join_thai_spaces("Google ไต"), "Google ไต");
    }

    #[test]
    fn ocr_blank_image_runs() {
        let api = load_api().expect("load api");
        let img: image::RgbImage =
            image::ImageBuffer::from_pixel(200, 60, image::Rgb([255u8, 255, 255]));
        let path = std::env::temp_dir().join("goat-test-blank.png");
        img.save(&path).expect("save test png");
        ocr_with(&api, path.to_str().expect("utf8 temp path")).expect("ocr blank image");
        let _ = std::fs::remove_file(&path);
    }
}
