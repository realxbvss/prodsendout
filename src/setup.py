from setuptools import setup, find_packages

setup(
    name="prodsendsout",
    version="0.1",
    packages=find_packages(),
    install_requires=[
        'aiogram==3.18.0',
        'google-api-python-client==2.163.0',
        'moviepy==1.0.3',
        'cryptography==44.0.2'
    ],
)