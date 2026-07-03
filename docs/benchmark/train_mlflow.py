"""Benchmark helper: log one small classifier run to MLflow.

Usage: MLFLOW_TRACKING_URI=sqlite:///mlflow.db python train_mlflow.py <learning_rate>
See docs/BENCHMARK.md for the full token-comparison methodology.
"""
import os
import sys

import mlflow
from sklearn.datasets import load_digits
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.model_selection import train_test_split

lr = float(sys.argv[1]) if len(sys.argv) > 1 else 0.01
Xtr, Xte, ytr, yte = train_test_split(
    *load_digits(return_X_y=True), test_size=0.3, random_state=0
)

mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
mlflow.set_experiment("mnist_like")
with mlflow.start_run(run_name=f"lr{lr}"):
    mlflow.log_param("learning_rate", lr)
    mlflow.log_param("model", "logreg")
    clf = LogisticRegression(C=lr, max_iter=2000).fit(Xtr, ytr)
    pred = clf.predict(Xte)
    proba = clf.predict_proba(Xte)
    mlflow.log_metric("accuracy", accuracy_score(yte, pred))
    mlflow.log_metric("val_loss", log_loss(yte, proba))
    mlflow.sklearn.log_model(clf, name="model")
    print(f"lr={lr} acc={accuracy_score(yte, pred):.4f}")
