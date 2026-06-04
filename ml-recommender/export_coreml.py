#!/usr/bin/env python3
"""
export_coreml.py — convert the trained model to Core ML for on-device inference.

The recommender's model (`model.json`) is a plain logistic regression:
a weight per feature, a bias, and the standardization (mean/std) used at train
time. That maps exactly onto a Core ML linear classifier, so a movie's affinity
can be scored entirely on-device (iPhone / Mac, no network, no Python).

This is OPTIONAL and experimental. It needs coremltools installed:

    pip install coremltools
    python3 recommend.py        # first, to produce model.json
    python3 export_coreml.py    # writes MovieAffinity.mlpackage

The exported model expects a feature vector in the order of model["vocab"]
(already standardized is handled internally here by folding mean/std into the
weights), and outputs P(like). On device you build the same feature dict from
TMDb metadata and apply it.
"""

import os
import json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "model.json")
OUT = os.path.join(HERE, "MovieAffinity.mlpackage")


def fold_standardization(model):
    """Fold (x-mu)/sd into raw weights so the Core ML model takes raw features.
       w_raw = theta/sd ; b_raw = bias - sum(theta*mu/sd)."""
    theta = np.array(model["theta"]); mu = np.array(model["mu"]); sd = np.array(model["sd"])
    w, b = theta[:-1], theta[-1]
    w_raw = w / sd
    b_raw = b - np.sum(w * mu / sd)
    return w_raw, float(b_raw)


def main():
    if not os.path.exists(MODEL_PATH):
        raise SystemExit("model.json not found — run recommend.py first (needs enough labels to train).")
    with open(MODEL_PATH) as f:
        model = json.load(f)
    if "vocab" not in model:
        raise SystemExit("model.json has no vocab — retrain with the current recommend.py.")

    try:
        import coremltools as ct
        from coremltools.models import datatypes
        from coremltools.models import MLModel
        from coremltools.models.pipeline import Pipeline  # noqa: F401
    except Exception:
        raise SystemExit("coremltools not installed. Run: pip install coremltools")

    w_raw, b_raw = fold_standardization(model)
    vocab = model["vocab"]

    # Build a minimal linear classifier spec (GLM logistic) over raw features.
    from coremltools.models import neural_network as nn  # lightweight path
    builder = nn.NeuralNetworkBuilder(
        input_features=[("features", datatypes.Array(len(vocab)))],
        output_features=[("like_probability", datatypes.Array(1))],
    )
    builder.add_inner_product(
        name="linear", input_name="features", output_name="logit",
        input_channels=len(vocab), output_channels=1,
        W=w_raw.reshape(1, -1), b=np.array([b_raw]), has_bias=True,
    )
    builder.add_activation(name="sigmoid", non_linearity="SIGMOID",
                           input_name="logit", output_name="like_probability")
    mlmodel = MLModel(builder.spec)
    mlmodel.short_description = "Personal movie affinity (P(like)) from TMDb features."
    mlmodel.user_defined_metadata["feature_order"] = json.dumps(vocab)
    mlmodel.save(OUT)
    print(f"Wrote {OUT}")
    print(f"Input: 'features' = vector of {len(vocab)} values in model['vocab'] order.")
    print("Output: 'like_probability'.")


if __name__ == "__main__":
    main()
