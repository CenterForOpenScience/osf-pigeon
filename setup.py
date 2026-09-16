import os
from setuptools import setup, find_packages


def parse_requirements(requirements):
    with open(requirements) as fp:
        return [
            line.strip("\n")
            for line in fp
            if line.strip("\n") and not line.startswith("#")
        ]


HERE = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(HERE, "README.md"), encoding="utf-8") as fp:
    long_description = fp.read()

setup(
    name="osf_pigeon",
    version="26.0.1",
    description="A utility for archiving osf storage projects at archive.org",
    long_description=long_description,
    author="Center for Open Science",
    author_email="contact@cos.io",
    install_requires=parse_requirements("requirements.txt"),
    url="https://github.com/CenterForOpenScience/osf-pigeon",
    packages=find_packages(exclude=("tests*",)),
    py_modules=["osf_pigeon.__main__"],
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        "Natural Language :: English",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.7",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
