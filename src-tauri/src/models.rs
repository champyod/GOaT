use std::path::PathBuf;

use pure_onnx_ocr_sync::{OcrEngine, OcrEngineBuilder};

// Placeholder local model paths. Models are never downloaded by the app;
// place the files here yourself. All paths are local only.
pub const MODELS_DIR: &str = "../models";

pub const OCR_DETECTION_MODEL: &str = "ppocrv5_mobile_det.onnx";
pub const OCR_RECOGNITION_MODEL: &str = "ppocrv5_mobile_rec.onnx";
pub const OCR_KEYS_FILE: &str = "ppocr_keys.txt";
pub const NLLB_MODEL_DIR: &str = "nllb-200-distilled-600M";

// Placeholder NLLB target language code (Flores-200 code).
pub const TRANSLATE_TARGET_LANG: &str = "tha_Thai";

#[derive(Clone, serde::Serialize)]
pub struct ModelsStatus {
    pub detection: bool,
    pub recognition: bool,
    pub keys: bool,
    pub nllb: bool,
    pub ready: bool,
}

pub fn models_dir() -> PathBuf {
    PathBuf::from(MODELS_DIR)
}

// Final builds bundle models with the app (resource dir) or place them in
// the app data dir. Dev placeholder comes last.
pub fn candidate_base_dirs(app: &tauri::AppHandle) -> Vec<PathBuf> {
    use tauri::Manager;
    let mut dirs = Vec::new();
    if let Ok(res) = app.path().resource_dir() {
        dirs.push(res.join("models"));
    }
    if let Ok(data) = app.path().app_data_dir() {
        dirs.push(data.join("models"));
    }
    dirs.push(models_dir());
    dirs
}

fn find_file(app: &tauri::AppHandle, name: &str) -> Option<PathBuf> {
    candidate_base_dirs(app)
        .into_iter()
        .map(|d| d.join(name))
        .find(|p| p.is_file())
}

pub fn models_status(app: &tauri::AppHandle) -> ModelsStatus {
    let detection = find_file(app, OCR_DETECTION_MODEL).is_some();
    let recognition = find_file(app, OCR_RECOGNITION_MODEL).is_some();
    let keys = find_file(app, OCR_KEYS_FILE).is_some();
    let nllb = candidate_base_dirs(app)
        .into_iter()
        .map(|d| d.join(NLLB_MODEL_DIR).join("model.bin"))
        .any(|p| p.is_file());
    ModelsStatus {
        detection,
        recognition,
        keys,
        nllb,
        ready: detection && recognition && keys && nllb,
    }
}

fn require_file(app: &tauri::AppHandle, name: &str, label: &str) -> anyhow::Result<PathBuf> {
    find_file(app, name).ok_or_else(|| {
        anyhow::anyhow!(
            "{label} model file {name} not found in bundled resources, app data dir, or {}",
            models_dir().display()
        )
    })
}

pub fn load_ocr_engine(app: &tauri::AppHandle) -> anyhow::Result<OcrEngine> {
    let det_path = require_file(app, OCR_DETECTION_MODEL, "detection")?;
    let rec_path = require_file(app, OCR_RECOGNITION_MODEL, "recognition")?;
    let keys_file = require_file(app, OCR_KEYS_FILE, "keys")?;
    let engine = OcrEngineBuilder::new()
        .det_model_path(&det_path)
        .rec_model_path(&rec_path)
        .dictionary_path(&keys_file)
        .build()
        .map_err(|e| anyhow::anyhow!("failed to build OCR engine: {e}"))?;
    Ok(engine)
}

pub fn run_ocr(engine: &OcrEngine, image: &image::DynamicImage) -> anyhow::Result<String> {
    let results = engine
        .run_from_image(image)
        .map_err(|e| anyhow::anyhow!("OCR inference failed: {e}"))?;
    let text = results
        .iter()
        .map(|r| r.text.trim())
        .filter(|t| !t.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    Ok(text)
}

// Fallback OCR via the tesseract sidecar exe (embedded eng+tha tessdata)
// when the primary tract engine errors for any reason. Runs out-of-process
// because tesseract's dynamic-CRT objects cannot link into this binary.
pub const OCR_SIDECAR_NAME: &str = "tesseract-ocr";

static OCR_TMP_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

pub async fn run_ocr_fallback(
    app: &tauri::AppHandle,
    image: &image::DynamicImage,
) -> anyhow::Result<String> {
    use tauri_plugin_shell::ShellExt;
    let n = OCR_TMP_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!("goat-ocr-{}-{n}.png", std::process::id()));
    image
        .save(&path)
        .map_err(|e| anyhow::anyhow!("failed to write temp image: {e}"))?;
    let out = app
        .shell()
        .sidecar(OCR_SIDECAR_NAME)
        .map_err(|e| anyhow::anyhow!("fallback OCR sidecar missing: {e}"))?
        .args([path.to_string_lossy().to_string()])
        .output()
        .await
        .map_err(|e| anyhow::anyhow!("fallback OCR spawn failed: {e}"))?;
    let _ = std::fs::remove_file(&path);
    if !out.status.success() {
        return Err(anyhow::anyhow!(
            "fallback OCR failed: {}",
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

pub fn load_translator(
    app: &tauri::AppHandle,
) -> anyhow::Result<ct2rs::Translator<ct2rs::tokenizers::auto::Tokenizer>> {
    let model_dir = candidate_base_dirs(app)
        .into_iter()
        .map(|d| d.join(NLLB_MODEL_DIR))
        .find(|d| d.join("model.bin").is_file())
        .ok_or_else(|| {
            anyhow::anyhow!(
                "translation model {NLLB_MODEL_DIR} not found in bundled resources, app data dir, or {}",
                models_dir().display()
            )
        })?;
    let translator = ct2rs::Translator::new(model_dir, &ct2rs::Config::default())?;
    Ok(translator)
}

pub fn run_translate(app: &tauri::AppHandle, text: &str) -> anyhow::Result<String> {
    let translator = load_translator(app)?;
    let sources = vec![text.to_string()];
    let target_prefixes = vec![vec![TRANSLATE_TARGET_LANG.to_string()]];
    let options = ct2rs::TranslationOptions::<String, String>::default();
    let results = translator.translate_batch_with_target_prefix(
        &sources,
        &target_prefixes,
        &options,
        None,
    )?;
    let out = results
        .into_iter()
        .map(|(s, _)| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    Ok(out)
}
