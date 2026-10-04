use std::path::PathBuf;

use pure_onnx_ocr_sync::{OcrEngine, OcrEngineBuilder};

// Last-resort model path, checked after the resource and app data directories.
// No such directory ships in the repository; the app never downloads models.
pub const MODELS_DIR: &str = "../models";

pub const OCR_DETECTION_MODEL: &str = "ppocrv5_mobile_det.onnx";
pub const OCR_RECOGNITION_MODEL: &str = "ppocrv5_mobile_rec.onnx";
pub const OCR_KEYS_FILE: &str = "ppocr_keys.txt";
pub const NLLB_MODEL_DIR: &str = "nllb-200-distilled-1.3B-ct2-int8";

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

// The bundle carries no resources key, so a final build ships no models and the
// resource dir is only ever empty. The app data dir comes before the dev path.
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

/// Translates one string through a translator the caller is already holding.
///
/// The load is a separate call from this one because it is the larger half of
/// the seam and is not translation: `load_translator` builds a translator on
/// every call and nothing is held between captures, so a caller that wants to
/// know what translating costs has to time the load and this apart itself. A
/// single function that did both could only ever report the sum.
pub fn run_translate_with(
    translator: &ct2rs::Translator<ct2rs::tokenizers::auto::Tokenizer>,
    text: &str,
) -> anyhow::Result<String> {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    /// The one candidate directory that is not derived from the running app, and
    /// the only path named in the error a missing model produces. A build that
    /// ships a bundle has no such directory, so this is also the path that tells
    /// a developer which checkout the app is looking in.
    #[test]
    fn the_last_resort_model_path_is_the_checkout_layout() {
        assert_eq!(MODELS_DIR, "../models");
        assert_eq!(models_dir(), PathBuf::from("../models"));
    }

    /// Every candidate directory is searched by joining one of these onto it. A
    /// name carrying a separator or a root would leave the directory it is
    /// supposed to come from, which is a different search from the one the three
    /// candidate directories are meant to describe.
    #[test]
    fn every_model_is_asked_for_by_a_name_of_its_own() {
        for name in [
            OCR_DETECTION_MODEL,
            OCR_RECOGNITION_MODEL,
            OCR_KEYS_FILE,
            NLLB_MODEL_DIR,
        ] {
            let path = Path::new(name);
            assert!(!path.is_absolute(), "{name} must be looked up by name");
            assert_eq!(
                path.components().count(),
                1,
                "{name} must be a bare name, not a path into a directory"
            );
        }
        assert_eq!(OCR_DETECTION_MODEL, "ppocrv5_mobile_det.onnx");
        assert_eq!(OCR_RECOGNITION_MODEL, "ppocrv5_mobile_rec.onnx");
        assert_eq!(OCR_KEYS_FILE, "ppocr_keys.txt");
        assert_eq!(NLLB_MODEL_DIR, "nllb-200-distilled-1.3B-ct2-int8");
    }

    /// The window reads this struct and switches on one field of it, and nothing
    /// at build time checks that the two still agree: a TypeScript type cannot
    /// see a Rust struct. A field renamed or retyped here leaves the panel
    /// waiting on a readiness that never arrives, so the shape travels as the
    /// contract it is.
    #[test]
    fn a_status_carries_the_fields_the_window_reads() {
        let status = ModelsStatus {
            detection: true,
            recognition: false,
            keys: true,
            nllb: false,
            ready: false,
        };

        let encoded = serde_json::to_value(&status).expect("a status serialises");
        let fields = encoded.as_object().expect("a status is an object");
        let mut names: Vec<&str> = fields.keys().map(String::as_str).collect();
        names.sort_unstable();
        assert_eq!(
            names,
            ["detection", "keys", "nllb", "ready", "recognition"],
            "the window reads `ready` off this struct and a rename would not be caught at build time"
        );
        for (name, field) in fields {
            assert!(field.is_boolean(), "{name} is read as a boolean");
        }
    }
}
