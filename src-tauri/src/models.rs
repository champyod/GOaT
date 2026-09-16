use std::path::PathBuf;

use ort::session::{builder::GraphOptimizationLevel, Session};

pub const MODELS_DIR: &str = "../models";

pub const OCR_DETECTION_MODEL: &str = "ppocrv5_mobile_det.onnx";
pub const OCR_RECOGNITION_MODEL: &str = "ppocrv5_mobile_rec.onnx";
pub const NLLB_MODEL_DIR: &str = "nllb-200-distilled-600M";

pub struct OcrModels {
    pub detection: Session,
    pub recognition: Session,
}

pub fn models_dir() -> PathBuf {
    PathBuf::from(MODELS_DIR)
}

pub fn load_ocr_models() -> anyhow::Result<OcrModels> {
    let detection_path = models_dir().join(OCR_DETECTION_MODEL);
    let recognition_path = models_dir().join(OCR_RECOGNITION_MODEL);

    let detection = Session::builder()?
        .with_optimization_level(GraphOptimizationLevel::Level3)?
        .with_intra_threads(4)?
        .commit_from_file(detection_path)?;

    let recognition = Session::builder()?
        .with_optimization_level(GraphOptimizationLevel::Level3)?
        .with_intra_threads(4)?
        .commit_from_file(recognition_path)?;

    Ok(OcrModels {
        detection,
        recognition,
    })
}

pub fn load_translator() -> anyhow::Result<ct2rs::Translator> {
    let model_dir = models_dir().join(NLLB_MODEL_DIR);
    let translator = ct2rs::Translator::new(model_dir, &ct2rs::Config::default())?;
    Ok(translator)
}