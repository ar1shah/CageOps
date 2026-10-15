"""Everything needed to turn stored features into a prediction, shared by training and serving.

Training (`cageops_trainer`) and the API (`cageops_api`) both import from here, so the two cannot
drift apart (training/serving skew). Nothing in this package knows how to *train* a model.

This file imports nothing: `asof` is stdlib-only so the worker can use it, while `features`,
`model`, `bundle` and `rows` need numpy and LightGBM (the `inference` extra).
"""
