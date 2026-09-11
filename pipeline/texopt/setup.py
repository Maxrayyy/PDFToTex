"""Compatibility shim for older pip/setuptools used on some servers."""

from setuptools import setup


setup(
    name="pdftotex-pipeline",
    version="0.1.0",
    description="Durable Lexoid LaTeX optimization and reviewed JSON pipeline",
    python_requires=">=3.10",
    packages=["texopt"],
    package_dir={"texopt": "."},
    entry_points={
        "console_scripts": [
            "texopt=texopt.cli:main",
            "texopt-pipeline=texopt.pipeline_daemon:main",
        ]
    },
)
