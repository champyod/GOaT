use std::path::PathBuf;

use paddleocr_rs_onnx::{OcrEngine, OrderBy};

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

pub fn detection_path() -> PathBuf {
    models_dir().join(OCR_DETECTION_MODEL)
}

pub fn recognition_path() -> PathBuf {
    models_dir().join(OCR_RECOGNITION_MODEL)
}

pub fn keys_path() -> PathBuf {
    models_dir().join(OCR_KEYS_FILE)
}

pub fn nllb_dir() -> PathBuf {
    models_dir().join(NLLB_MODEL_DIR)
}

pub fn models_status() -> ModelsStatus {
    let detection = detection_path().is_file();
    let recognition = recognition_path().is_file();
    let keys = keys_path().is_file();
    let nllb = nllb_dir().join("model.bin").is_file();
    ModelsStatus {
        detection,
        recognition,
        keys,
        nllb,
        ready: detection && recognition && keys && nllb,
    }
}

fn read_model_file(path: &PathBuf, label: &str) -> anyhow::Result<Vec<u8>> {
    std::fs::read(path).map_err(|_| {
        anyhow::anyhow!(
            "{label} model file not found at {} (placeholder path, place the file there)",
            path.display()
        )
    })
}

pub fn load_ocr_engine() -> anyhow::Result<OcrEngine> {
    let det_model = read_model_file(&detection_path(), "detection")?;
    let rec_model = read_model_file(&recognition_path(), "recognition")?;
    let keys_data = read_model_file(&keys_path(), "keys")?;
    let engine = OcrEngine::new(&det_model, &rec_model, &keys_data)
        .map_err(|e| anyhow::anyhow!("failed to build OCR engine: {e}"))?;
    Ok(engine)
}

pub fn run_ocr(engine: &OcrEngine, image: &image::DynamicImage) -> anyhow::Result<String> {
    let blocks = engine
        .recognize_all(image, OrderBy::Horizontal)
        .map_err(|e| anyhow::anyhow!("OCR inference failed: {e}"))?;
    let text = blocks
        .iter()
        .map(|b| b.text.trim())
        .filter(|t| !t.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    Ok(text)
}

pub fn load_translator() -> anyhow::Result<ct2rs::Translator<ct2rs::tokenizers::auto::Tokenizer>> {
    let model_dir = nllb_dir();
    if !model_dir.join("model.bin").is_file() {
        return Err(anyhow::anyhow!(
            "translation model not found at {} (placeholder path, place the converted CTranslate2 model there)",
            model_dir.display()
        ));
    }
    let translator = ct2rs::Translator::new(model_dir, &ct2rs::Config::default())?;
    Ok(translator)
}

pub fn run_translate(text: &str) -> anyhow::Result<String> {
    let translator = load_translator()?;
    let sources = vec![text.to_string()];
    let target_prefixes = vec![vec![TRANSLATE_TARGET_LANG.to_string()]];
    let options = ct2rs::TranslationOptions::<String, String>::default();
    let results =
        translator.translate_batch_with_target_prefix(&sources, &target_prefixes, &options, None)?;
    let out = results
        .into_iter()
        .map(|(s, _)| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    Ok(out)
}