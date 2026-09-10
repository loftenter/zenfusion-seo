from setuptools import setup, find_packages

setup(
    name="zenfusion-seo",
    version="1.0.0",
    description="Open-source SEO/AEO/CRO toolkit for Shopify stores — 67+ analysis scripts",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Zenfusion",
    author_email="justin@zenfusion.com",
    url="https://github.com/loftenter/zenfusion-seo",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "flask>=2.0",
        "requests>=2.28",
    ],
    entry_points={
        "console_scripts": [
            "zenfusion-seo=zenfusion_seo.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Internet :: WWW/HTTP :: Site Management",
    ],
)
