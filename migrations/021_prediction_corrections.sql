-- Append-only display corrections for immutable published live predictions.
-- Idempotent: safe to run more than once on bond_db.

CREATE TABLE IF NOT EXISTS t_scheme_prediction_corrections (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    prediction_id BIGINT NOT NULL,
    scheme_id VARCHAR(64) NOT NULL,
    target_tenor VARCHAR(16) NOT NULL,
    horizon INT NOT NULL,
    predict_date DATE NOT NULL,
    feature_date DATE NOT NULL,
    target_date DATE NOT NULL,
    original_direction TINYINT NOT NULL,
    corrected_direction TINYINT NOT NULL,
    scheme_version VARCHAR(64) NOT NULL,
    prediction_phase VARCHAR(32) NOT NULL,
    correction_reason VARCHAR(64) NOT NULL,
    operation_id VARCHAR(128) NOT NULL,
    operation_scope_sha256 CHAR(64) NOT NULL,
    release_commit CHAR(40) NOT NULL,
    evidence JSON NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_prediction_correction_prediction (prediction_id),
    UNIQUE KEY uk_prediction_correction_business (
        scheme_id, target_tenor, horizon, target_date
    ),
    UNIQUE KEY uk_prediction_correction_operation (operation_id),
    UNIQUE KEY uk_prediction_correction_scope (operation_scope_sha256),
    CONSTRAINT fk_prediction_correction_prediction
        FOREIGN KEY (prediction_id) REFERENCES t_scheme_predictions(id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_prediction_correction_original_direction
        CHECK (original_direction IN (-1, 0, 1)),
    CONSTRAINT chk_prediction_correction_corrected_direction
        CHECK (corrected_direction IN (-1, 0, 1)),
    CONSTRAINT chk_prediction_correction_changes_direction
        CHECK (original_direction <> corrected_direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
