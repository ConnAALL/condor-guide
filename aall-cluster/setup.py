"""Compatibility metadata for the older setuptools installed on mega-knight."""

from setuptools import find_packages, setup


setup(
    name="aall-cluster",
    version="0.1.0",
    description="A colorful terminal monitor for the AALL HTCondor cluster.",
    python_requires=">=3.9",
    packages=find_packages(),
    entry_points={"console_scripts": ["aall-cluster=aall_cluster.__main__:main"]},
)
