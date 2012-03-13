#!/usr/bin/python
# Copyright 2010 Shal Dengeki
# Licensed under the WTF Public License, Version 2.0
# http://sam.zoy.org/wtfpl/COPYING
# Provides some basic topic- and post-classification functions for ETI.

import MySQLdb
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.feature_extraction.text import WordNGramAnalyzer
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.pipeline import Pipeline
from sklearn.naive_bayes import MultinomialNB
from sklearn.linear_model import SGDClassifier
from sklearn.svm.sparse import LinearSVC
from sklearn import metrics
import pickle
import numpy as np
from BeautifulSoup import BeautifulSoup

def getPostData(username="", password="", database=""):
  X = []
  y = []
  
  dbConn = MySQLdb.connect('localhost', username, password, database, use_unicode=True)
  dbCursor = dbConn.cursor()
  
  # first get lists of raw counts, and targets.
  dbCursor.execute('SELECT `message_text`, `post_type` FROM `posts` WHERE `message_text` != "NULL"')
  
  post = dbCursor.fetchone()
  while post is not None:
    X.append(post[0])
    y.append(post[1])
    post = dbCursor.fetchone()
    
  return (X, y)
  
def stripPostHTML(postText):
  '''
  Returns a string with all HTML strings stripped.
  '''
  if postText is None:
    return ''
  return "".join([line.strip() for line in ''.join(BeautifulSoup(unicode(postText), fromEncoding='utf-8').findAll(text=True)).split("\n")])
  
def getTopicData(tagID, userID=0, username="", password="", database=""):
  '''
  Returns a list of (topic titles appended to topic OPs) for the given tagID and userID (if specified).
  '''
  X = []
  y = []
  
  dbConn = MySQLdb.connect('localhost', username, password, database, use_unicode=True)
  trainingDataCursor = dbConn.cursor()
  topicDataCursor = dbConn.cursor()
  
  
  # first get lists of raw counts, and targets.
  if userID is not 0:
    trainingDataCursor.execute(u'''SELECT `topicid`, `type` FROM `tags_training` WHERE (`tagid` = %s && `userid` = %s)''', [str(tagID), str(userID)])
  else:
    trainingDataCursor.execute(u'''SELECT `topicid`, `type` FROM `tags_training` WHERE (`tagid` = %s)''', [str(tagID)])
  
  topic = trainingDataCursor.fetchone()
  while topic is not None:
    # get this topic's first post and title.
    topicDataCursor.execute(u'''SELECT `topics`.`title`, `posts`.`messagetext` FROM `topics` LEFT OUTER JOIN `posts` ON `topics`.`ll_topicid` = `posts`.`ll_topicid` WHERE `topics`.`ll_topicid` = %s ORDER BY `posts`.`date` ASC LIMIT 1''', [str(topic[0])])
    topicInfo = topicDataCursor.fetchone()
    if topicInfo is not None:
      topicData = [stripPostHTML(text) for text in topicInfo]
      if topicData[1] is None:
        X.append(topicData[0])
      else:
        X.append(" ".join(topicData))
      y.append(topic[1])
    topic = trainingDataCursor.fetchone()
    
  return (X, y)
  
def transformData(X,nGram=1,count_vect=False,tfidf_transformer=False):
  # vectorize the text into features and compute tf*idf.
  if not count_vect:
    count_vect = CountVectorizer(analyzer=WordNGramAnalyzer(max_n=nGram))
    X_train_counts = count_vect.fit_transform(X)
  else:
    X_train_counts = count_vect.transform(X)
  if not tfidf_transformer:
    tfidf_transformer = TfidfTransformer()
    X_train_tfidf = tfidf_transformer.fit_transform(X_train_counts)
  else:
    X_train_tfidf = tfidf_transformer.transform(X_train_counts)    
  
  return (X_train_tfidf,count_vect,tfidf_transformer)
  
def shuffle_in_unison(a, b):
  '''
  Shuffles two lists in unison.
  '''
  rng_state = np.random.get_state()
  np.random.shuffle(a)
  np.random.set_state(rng_state)
  np.random.shuffle(b)
  
def trainClassifiers(X, y, classifiers=["naivebayesian", "l1log", "l2log", "linearsvc"]):
  '''
  Takes a sparse compressed array X of feature values and an array y of label assignments and returns naive bayesian, L1,L2 SGD logistic, and SVM classifiers.
  '''
  returnClassifiers = []
  if "naivebayesian" in classifiers:
    returnClassifiers.append(MultinomialNB().fit(X,y))
  if "l1log" in classifiers:
    returnClassifiers.append(SGDClassifier(loss="log", penalty="l1",shuffle=True,seed=42).fit(X.toarray(), y))
  if "l2log" in classifiers:
    returnClassifiers.append(SGDClassifier(loss="log", penalty="l2",shuffle=True,seed=42).fit(X.toarray(), y))
  if "linearsvc" in classifiers:
    returnClassifiers.append(LinearSVC().fit(X,y))
  return tuple(returnClassifiers)
  
def classifyDocuments(docs, clf, count_vect, tfidf_transformer):
  X_new_counts = count_vect.transform(docs)
  X_new_tfidf = tfidf_transformer.transform(X_new_counts)
  predicted = clf.predict(X_new_tfidf)
  classifiedDocs = {}
  for doc, category in zip(docs, predicted):
    classifiedDocs[doc] = category
  return classifiedDocs
  
def getClassifierPerformance(classifiers, X, y):
  output = []
  for classifier in classifiers:
    predicted = classifier.predict(X)
    output.append(metrics.classification_report(y, predicted))
  return "\r\n".join(output)
    
def dumpClassifier(classifier,count_vect, tfidf_transformer):
  outputFile = open('clf.txt', 'w')
  outputFile.write(pickle.dumps(classifier))
  outputFile.close()
  
  outputFile = open('count_vect.txt', 'w')
  outputFile.write(pickle.dumps(count_vect))
  outputFile.close()
  
  outputFile = open('tfidf_transformer.txt', 'w')
  outputFile.write(pickle.dumps(tfidf_transformer))
  outputFile.close()
  
def regenerateClassifier(database="seinma_lltrolls_dev", nGram=1):
  (X,y) = getData(database)
  (X_train_tfidf,count_vect,tfidf_transformer) = transformData(X)
  (bayesianClassifier,l1Classifier, l2Classifier, svcClassifier) = trainClassifiers(X_train_tfidf, y)
  dumpClassifier(l2Classifier,count_vect,tfidf_transformer)