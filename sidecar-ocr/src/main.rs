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
    // Prefer eng+tha; if Thai data isn't embedded, degrade to eng alone
    // instead of failing outright.
    match init_api("eng+tha") {
        Ok(api) => ocr_with(&api, &path),
        Err(_) => ocr_with(&init_api("eng")?, &path),
    }
}

fn init_api(langs: &str) -> anyhow::Result<tesseract_rs::TesseractAPI> {
    let api = tesseract_rs::TesseractAPI::new();
    api.init_embedded(langs)
        .map_err(|e| anyhow::anyhow!("tesseract init failed: {e}"))?;
    Ok(api)
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
    print!("{text}");
    Ok(())
}
