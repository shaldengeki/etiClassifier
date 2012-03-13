try:
  from setuptools import setup
except ImportError:
  from distutils.core import setup

config = {
  'description': 'etiClassifier', 
  'author': 'Shal Dengeki', 
  'url': 'https://github.com/shaldengeki/eticlassifier', 
  'download_url': 'git@github.com:shaldengeki/eticlassifier.git', 
  'author_email': 'shaldengeki@gmail.com', 
  'version': '0.1', 
  'install_requires': ['nose'], 
  'packages': ['etiClassifier'], 
  'scripts': [],
  'name': 'etiClassifier'
}

setup(**config)