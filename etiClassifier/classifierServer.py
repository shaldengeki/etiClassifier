#!/usr/bin/env python
import datetime
import etiClassifier
import json
import math
import MySQLdb
import pickle
import re
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.feature_extraction.text import TfidfTransformer
import sys
import syslog
import time
import urllib2
import web

urls = (
  '/', 'mainPage',
  '/post/([a-zA-Z]+)/?',  'post',
  '/topic/([a-zA-Z]+)/?', 'topic',
  '/tag/([a-zA-Z]+)/?', 'tag', 
  '/tagging/([a-zA-Z]+)/?', 'tagging', 
  '/subscription/([a-zA-Z]+)/?', 'subscription'
)

app = web.application(urls, globals())

class mainPage:
  def GET(self):
    return "This is the classification daemon powering ETITroll and ETITags. Resources are availble at /post/, /topic/, /tag/, and /subscription/."

class post:
  def reloadClassifier(self):
    try:
      inputFile = open('clf.txt', 'r')
      clfString = inputFile.read()
      inputFile.close()
      web.trollClassifier = pickle.loads(clfString)

      inputFile = open('count_vect.txt', 'r')
      countVectString = inputFile.read()
      inputFile.close()
      web.trollCountVectorizer = pickle.loads(countVectString)

      inputFile = open('tfidf_transformer.txt', 'r')
      tfIdfString = inputFile.read()
      inputFile.close()
      web.trollTfIdfTransformer = pickle.loads(tfIdfString)
    except:
      return "Could not load classifier files."
    return "Classifier reloaded."
  
  def GET(self, action):
    if action == 'benchmark':
      # display performance statistics for the current classifier.
      (X,y) = etiClassifier.getPostData(username=web.MYSQL_USERNAME, password=web.MYSQL_PASSWORD, database='seinma_lltrolls')
      splitDataPoint = int(math.floor(7*len(X)/10))
      
      (X_train_tfidf,count_vect,tfidf_transformer) = etiClassifier.transformData(X, count_vect=web.trollCountVectorizer, tfidf_transformer=web.trollTfIdfTransformer)
      return etiClassifier.getClassifierPerformance([classifier for classifier in web.trollClassifier], X_train_tfidf[splitDataPoint:], y[splitDataPoint:])
    elif action == 'regenerate':
      # pull data from the database and recalculate the classifier parameters.
      (X,y) = etiClassifier.getPostData(username=web.MYSQL_USERNAME, password=web.MYSQL_PASSWORD, database='seinma_lltrolls')
      splitDataPoint = int(math.floor(7*len(X)/10))
      
      (X_train_tfidf,count_vect,tfidf_transformer) = etiClassifier.transformData(X, nGram=2)
      (bayesianClassifier,l1Classifier, l2Classifier, svcClassifier) = etiClassifier.trainClassifiers(X_train_tfidf[0:splitDataPoint], y[0:splitDataPoint])
      etiClassifier.dumpClassifier(l2Classifier,count_vect,tfidf_transformer)
      web.trollCountVectorizer = count_vect
      web.trollTfIdfTransformer = tfidf_transformer
      web.trollClassifier = (l1Classifier, svcClassifier, l2Classifier, bayesianClassifier)
      return "Classifier regenerated."
    else:
      return "Please specify a valid action."
  
  def POST(self, action):
    if action == 'report':
      # process a troLLreporter report submission.
      return "Coming soon!"
    elif action == 'identify':
      # process a troLLidentifier identification request.
      postFields = web.input(name = 'web')
      if 'text' not in postFields:
        return "Please provide some text for analysis."
      else:
        transformedText = etiClassifier.transformData([etiClassifier.stripPostHTML(postFields.text)], count_vect=web.trollCountVectorizer, tfidf_transformer=web.trollTfIdfTransformer)[0]
        return str(round(100*web.trollClassifier[0].predict_proba(transformedText)[0], 2))
    else:
      return "Please specify a valid action."

class topic:
  def checkDBConnection(self):
    if datetime.datetime.now() > web.lastQueryTime + datetime.timedelta(minutes=60):
      web.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      web.dbCursor = web.dbConn.cursor()
    web.lastQueryTime = datetime.datetime.now()
  
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    web.dbCursor.execute(u'''SELECT COUNT(*) FROM `authAttempts` WHERE (`ip` = %s && `date` >= %s && `type` = 0)''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(web.dbCursor.fetchone()[0])
    if authAttempts > 3:
      return False
    
    # see if this request's IP matches the last IP in our users table.
    web.dbCursor.execute(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if web.dbCursor.rowcount < 1:
      return False
    userInfo = web.dbCursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        web.dbCursor.execute(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        web.dbCursor.execute(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        return userInfo
    else:
      return userInfo
  def GET(self, action):
    if action == 'classify':
      '''
      Classifies a user-specified list of topics.
      Returns a JSON list of topic objects, containing a list of tags belonging to those topics.
      '''
      getFields = web.input(name = 'web')
      
      # check to make sure all the fields are here.
      if 'topics' not in getFields:
        return -1
      try:
        getFields.topics = json.loads(getFields.topics)
        topics = [int(topicID) for topicID in getFields.topics]
      except (TypeError, ValueError):
        # malformed JSON, topics not iterable, or topicID not numeric.
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
        
      if 'username' in getFields:
        # user is requesting just the tags s/he is subscribed to.
        # first, authenticate this user.
        userInfo = self.authenticateUser(getFields.username)
        if not userInfo:
          return -2
        # now load the relevant tags.
        web.dbCursor.execute(u'''SELECT `subscriptions`.`tagid`, `tags`.`name`, `tags`.`classifier`, `tags`.`countVectorizer`, `tags`.`tfidfTransformer`, 
          (
            SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
          ) AS `subscriptionCount` FROM `subscriptions` LEFT OUTER JOIN `tags` ON `subscriptions`.`tagid` = `tags`.`tagid` WHERE (`subscriptions`.`userid` = %s AND LENGTH(`tags`.`classifier`) > 0) ORDER BY `subscriptionCount` DESC''', [str(userInfo[0])])
        relevantTags = {}
        tag = web.dbCursor.fetchone()
        while tag is not None:
          try:
            relevantTags[tag[0]] = (tag[1], pickle.loads(tag[2]), pickle.loads(tag[3]), pickle.loads(tag[4]), int(tag[5]))
          except EOFError:
            # this tag is missing a classifier / count vectorizer / tfidf transformer field. skip it.
            tag = web.dbCursor.fetchone()
            continue
          tag = web.dbCursor.fetchone()
      else:
        # user is requesting all public tags.
        relevantTags = web.publicTags
        
      returnList = []
      for topicID in topics:
        topicDict = {'id': topicID}
        tagList = []
        # get topic title and OP.
        web.dbCursor.execute(u'''SELECT `topics`.`title`, `posts`.`messagetext` FROM `topics` LEFT OUTER JOIN `posts` ON `topics`.`ll_topicid` = `posts`.`ll_topicid` WHERE `topics`.`ll_topicid` = %s ORDER BY `posts`.`date` ASC LIMIT 1''', [str(topicID)])
        topicInfo = web.dbCursor.fetchone()
        if topicInfo is not None:
          topicString = " ".join([unicode(text) for text in topicInfo])
          for tagID in relevantTags:
            transformedText = etiClassifier.transformData([etiClassifier.stripPostHTML(topicString)], count_vect=relevantTags[tagID][2], tfidf_transformer=relevantTags[tagID][3])[0]
            tagList.append({'id': tagID, 'name': relevantTags[tagID][0], 'prob': round(relevantTags[tagID][1][0].predict_proba(transformedText)[0], 4), 'subscriptionCount': relevantTags[tagID][4]})
          
          topicDict['tags'] = tagList
          returnList.append(topicDict)
        else:
          # this topic isn't in our db. return an empty taglist for now.
          returnList.append({'id': topicID, 'tags': []})
      return json.dumps(returnList)       

    else:
      return "Please specify a valid action."
    
  def POST(self, action):
    if action == 'classify':
      '''
      Updates a tag classifier with a new topic classification.
      Returns standard error codes.
      '''
      postFields = web.input(name = 'web')
      # check to make sure all the fields are here.
      if 'tag' not in postFields or 'username' not in postFields or 'topic' not in postFields or 'type' not in postFields or (postFields.type != "0" and postFields.type != "1"):
        return -1
      try:
        postFields.topic = int(postFields.topic)
        postFields.type = int(postFields.type)
      except ValueError:
        # one or more of these is not a numeric string.
        return -1
      if len(postFields.tag) < 3 or postFields.topic <= 0 or (postFields.type != 0 and postFields.type != 1):
        # specific values of these parameters is incorrect.
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2

      # user is authenticated. proceed with classification submission.
      # see if this tag already exists in the table.
      web.dbCursor.execute(u'''SELECT `tagid`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` WHERE `userid` = %s AND `name` = %s LIMIT 1''', [str(userInfo[0]), str(postFields.tag)])
      if web.dbCursor.rowcount < 1:
        # new tag. insert and set tagid to be this last-inserted ID.
        web.dbCursor.execute(u'''INSERT INTO `tags` (`name`, `userid`, `classifier`, `countVectorizer`, `tfidfTransformer`) VALUES (%s, %s, '', '', '')''', [str(postFields.tag), str(userInfo[0])])
        tagInfo = (web.dbConn.insert_id(), 1)
        # subscribe this user to this tag as well.
        web.dbCursor.execute(u'''INSERT INTO `subscriptions` (`userid`, `tagid`) VALUES (%s, %s)''', [str(userInfo[0]), str(tagInfo[0])])
      else:
        # pre-existing tag. set tagid to be this ID.
        tagInfo = web.dbCursor.fetchone()
      # insert this training entry.
      #try:
      web.dbCursor.execute(u'''INSERT INTO `tags_training` (`tagid`, `topicid`, `type`, `userid`) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE `type` = %s''', [str(tagInfo[0]), str(postFields.topic), str(postFields.type), str(userInfo[0]), str(postFields.type)])
#      except MySQLdb.IntegrityError:
        # duplicate training example.
       # tagInfo = tagInfo
      
      # update classifier with new data.
      (X, y) = etiClassifier.getTopicData(tagInfo[0], username=web.MYSQL_USERNAME, password=web.MYSQL_PASSWORD, database=web.MYSQL_DATABASE)
      if len(X) > 1 and 0 in y and 1 in y:
        etiClassifier.shuffle_in_unison(X, y)
        (X_train_tfidf,count_vect,tfidf_transformer) = etiClassifier.transformData(X)
        (classifier) = etiClassifier.trainClassifiers(X_train_tfidf, y, classifiers=["l2log"])
        web.dbCursor.execute(u'''UPDATE `tags` SET `classifier` = %s, `countVectorizer` = %s, `tfidfTransformer` = %s WHERE `tagid` = %s LIMIT 1''', [pickle.dumps(classifier), pickle.dumps(count_vect), pickle.dumps(tfidf_transformer), str(tagInfo[0])])
      
      return json.dumps({'id': tagInfo[0], 'name': postFields.tag, 'subscriptionCount': tagInfo[1]})
    else:
      return "Please specify a valid action."
    
class tag:
  def checkDBConnection(self):
    if datetime.datetime.now() > web.lastQueryTime + datetime.timedelta(minutes=60):
      web.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      web.dbCursor = web.dbConn.cursor()
    web.lastQueryTime = datetime.datetime.now()
  
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    web.dbCursor.execute(u'''SELECT COUNT(*) FROM `authAttempts` WHERE `ip` = %s AND `date` >= %s''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(web.dbCursor.fetchone()[0])
    if authAttempts > 3:
      return False
    # see if this request's IP matches the last IP in our users table.
    web.dbCursor.execute(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if web.dbCursor.rowcount < 1:
      return False
    userInfo = web.dbCursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        web.dbCursor.execute(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        web.dbCursor.execute(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        return userInfo
    else:
      return userInfo
  def GET(self, action):
    getFields = web.input(name = 'web')
    if action == 'show':
      '''
      Displays a list of topics belonging to a tag.
      '''
      # check to ensure that request is well-formed and that user is authenticated to view this tag.
      if 'tag' not in getFields or 'username' not in getFields:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      web.dbCursor.execute(u'''SELECT `userid`, `public` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(getFields.tag)])
      if web.dbCursor.rowcount < 1:
        return -3
      tagInfo = web.dbCursor.fetchone()
      if tagInfo[0] != userInfo[0] and tagInfo[1] == "0":
        return -4
      # otherwise, go ahead with the display.
      web.dbCursor.execute(u'''SELECT `topics`.`ll_topicid`, `topics`.`title`, `topics`.`userid`, `ll_users`.`username`, 
      (
        SELECT COUNT(*) FROM `posts` WHERE `posts`.`ll_topicid` = `topics`.`ll_topicid`
      ) AS `postCount`, 
      (
        SELECT `date` FROM `posts` WHERE `posts`.`ll_topicid` = `topics`.`ll_topicid` ORDER BY `date` DESC LIMIT 1
      ) AS `lastPostTime` FROM `topics` LEFT OUTER JOIN `taggings` ON `taggings`.`ll_topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` ON `topics`.`userid` = `ll_users`.`userid` WHERE `taggings`.`tagid` = %s''', [str(getFields.tag)])
      taggingCursor = web.dbConn.cursor()
      taggedTopics = []
      topicInfo = web.dbCursor.fetchone()
      while topicInfo is not None:
        # get this topic's tag info.
        taggingCursor.execute(u'''SELECT `taggings`.`tagid`, `tags`.`name`, `taggings`.`prob`, `tags`.`userid` FROM `taggings` LEFT OUTER JOIN `tags` ON `tags`.`tagid` = `taggings`.`tagid` WHERE `taggings`.`ll_topicid` = %s''', [str(topicInfo[0])])
        topicTags = [{'id': taggingInfo[0], 'name': taggingInfo[1], 'prob': taggingInfo[2]} for taggingInfo in taggingCursor.fetchall()]
        taggedTopics.append({'id': topicInfo[0], 'title': topicInfo[1], 'creator': {'id': topicInfo[2], 'username': topicInfo[3]}, 'postCount': topicInfo[4], 'lastPostTime': topicInfo[5], 'tags': topicTags})
        topicInfo = web.dbCursor.fetchone()
      return json.dumps(taggedTopics)
    
    elif action == 'owned':
      '''
      Takes a user's name and returns a JSON list of tag objects belonging to this user.
      '''
      # validate input fields and user auth.
      if 'username' not in getFields:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # otherwise go ahead and list these tags.
      web.dbCursor.execute(u'''SELECT `tagid`, `name`, `public` FROM `tags` WHERE `userid` = %s''', [str(userInfo[0])])
      ownedTags = [{'id': tagInfo[0], 'name': tagInfo[1], 'public': bool(int(tagInfo[2]))} for tagInfo in web.dbCursor.fetchall()]
      return json.dumps(ownedTags)
    
    elif action == 'search':
      '''
      Performs a search amongst public tags.
      Returns a JSON list of tag objects.
      '''
      # authenticate user.
      if 'username' not in getFields:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      
      if 'name' in getFields:
        # search for this particular tag name.
        searchTerms = getFields.name.split()

        web.dbCursor.execute(u'''SELECT `tags`.`tagid`, `tags`.`name`, (
            SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
          ) AS `subscriptionCount` FROM `tags` WHERE (`tags`.`public` = 1 OR `tags`.`userid` = %s) AND MATCH(`tags`.`name`) AGAINST(%s IN BOOLEAN MODE) ORDER BY `subscriptionCount` DESC''', [str(userInfo[0]), str(getFields.name)])

        return json.dumps([{'id': tag[0], 'name':tag[1], 'subscriptionCount':tag[2]} for tag in web.dbCursor.fetchall()])
      else:
        # just get all public tags, which we conveniently have in memory.
        return json.dumps([{'id': tag, 'name':web.publicTags[tag][0], 'subscriptionCount':web.publicTags[tag][4]} for tag in web.publicTags])
        
    else:
      return "Please specify a valid action."
  
  def POST(self, action):
    postFields = web.input(name = 'web')
    if action == 'edit':
      '''
      Modifies attributes of a tag.
      Returns a standard error code.
      '''
      # validate input and authenticate user.
      if 'tag' not in postFields or 'username' not in postFields or 'public' not in postFields:
        return -1
      try:
        postFields.tag = int(postFields.tag)
        postFields.public = int(postFields.public)
      except (TypeError, ValueError):
        return -1
      if postFields.tag <= 0 or (postFields.public != 0 and postFields.public != 1):
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      web.dbCursor.execute(u'''SELECT `tags`.`userid`, `tags`.`public`, `tags`.`name`, `tags`.`classifier`, `tags`.`countVectorizer`, `tags`.`tfidfTransformer`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      if web.dbCursor.rowcount < 1:
        return -3
      tagInfo = web.dbCursor.fetchone()
      if tagInfo[0] != userInfo[0] and tagInfo[1] == "0":
        return -4
      # go ahead with the modification.
      try:
        web.dbCursor.execute(u'''UPDATE `tags` SET `public` = %s WHERE `tagid` = %s LIMIT 1''', [str(postFields.public), str(postFields.tag)])
        if postFields.public is 1 and str(postFields.tag) not in web.publicTags:
          web.publicTags[str(postFields.tag)] = (tagInfo[2], tagInfo[3], tagInfo[4], tagInfo[5], tagInfo[6])
        elif postFields.public is 0 and str(postFields.tag) in web.publicTags:
          del(web.publicTags[str(postFields.tag)])
      except:
        return 0
      return 1
    elif action == 'delete':
      '''
      Deletes a tag belonging to a user.
      Returns a standard error code.
      '''
      # validate input and authenticate user.
      if 'tag' not in postFields or 'username' not in postFields:
        return -1
      try:
        postFields.tag = int(postFields.tag)
      except (TypeError, ValueError):
        return -1
      if postFields.tag <= 0:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user.
      web.dbCursor.execute(u'''SELECT `userid`, `public` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      if web.dbCursor.rowcount < 1:
        return -3
      tagInfo = web.dbCursor.fetchone()
      if tagInfo[0] != userInfo[0]:
        return -4
      # go ahead with the modification.
      try:
        # first, delete all the subscriptions belonging to this tag.
        web.dbCursor.execute(u'''DELETE FROM `subscriptions` WHERE `tagid` = %s''', [str(postFields.tag)])
        # second, delete all taggings belonging to this tag.
        web.dbCursor.execute(u'''DELETE FROM `taggings` WHERE `tagid` = %s''', [str(postFields.tag)])
        # third, delete all the training data belonging to this tag.
        web.dbCursor.execute(u'''DELETE FROM `tags_training` WHERE `tagid` = %s''', [str(postFields.tag)])
        # fourth, remove this tag from publicTags (if it needs to be removed).
        if tagInfo[1] is "1":
          del(web.publicTags[str(postFields.tag)])        
        # finally, delete the tag itself.
        web.dbCursor.execute(u'''DELETE FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      except:
        return 0
      return 1
      
    else:
      return "Please specify a valid action."

class tagging:
  def checkDBConnection(self):
    if datetime.datetime.now() > web.lastQueryTime + datetime.timedelta(minutes=60):
      web.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      web.dbCursor = web.dbConn.cursor()
    web.lastQueryTime = datetime.datetime.now()
  
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    web.dbCursor.execute(u'''SELECT COUNT(*) FROM `authAttempts` WHERE `ip` = %s AND `date` >= %s''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(web.dbCursor.fetchone()[0])
    if authAttempts > 3:
      return False
    # see if this request's IP matches the last IP in our users table.
    web.dbCursor.execute(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if web.dbCursor.rowcount < 1:
      return False
    userInfo = web.dbCursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        web.dbCursor.execute(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        web.dbCursor.execute(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        return userInfo
    else:
      return userInfo
  def GET(self, action):
    getFields = web.input(name = 'web')
    if action == 'list':
      '''
      Displays a list of training taggings that the user has submitted.
      '''
      # check to ensure that request is well-formed and that user is authenticated to view this tag.
      if 'username' not in getFields:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # get a list of taggings for this user (and if topic is provided, restrict to just that topic.)
      if 'topic' in getFields:
        try:
          getFields.topic = int(getFields.topic)
        except (TypeError, ValueError):
          return -1
        web.dbCursor.execute(u'''SELECT `tags_training`.`trainingid`, `tags_training`.`tagid`, `tags`.`name`, `tags_training`.`topicid`, `topics`.`title`, `topics`.`userid`, `ll_users`.`username`, `tags_training`.`type` FROM `tags_training` LEFT OUTER JOIN `tags` ON `tags_training`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `topics` ON `tags_training`.`topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `topics`.`userid` WHERE (`tags_training`.`userid` = %s AND `tags_training`.`topicid` = %s)''', [str(userInfo[0]), str(getFields.topic)])
      else:
        web.dbCursor.execute(u'''SELECT `tags_training`.`trainingid`, `tags_training`.`tagid`, `tags`.`name`, `tags_training`.`topicid`, `topics`.`title`, `topics`.`userid`, `ll_users`.`username`, `tags_training`.`type` FROM `tags_training` LEFT OUTER JOIN `tags` ON `tags_training`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `topics` ON `tags_training`.`topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `topics`.`userid` WHERE `tags_training`.`userid` = %s''', [str(userInfo[0])])
      trainingList = []
      trainingInfo = web.dbCursor.fetchone()
      while trainingInfo is not None:
        trainingList.append({'id': int(trainingInfo[0]), 'tag':{'id': int(trainingInfo[1]), 'name': trainingInfo[2]}, 'topic':{'id': int(trainingInfo[3]), 'title': trainingInfo[4], 'creator': {'id': int(trainingInfo[5]), 'username': trainingInfo[6]}}, 'type': int(trainingInfo[7])})
        trainingInfo = web.dbCursor.fetchone()
      return json.dumps(trainingList)
    
    else:
      return "Please specify a valid action."
  
  def POST(self, action):
    postFields = web.input(name = 'web')
    if action == 'edit':
      '''
      Modifies attributes of a training tagging.
      Returns a standard error code.
      '''
      # validate input and authenticate user.
      if 'tagging' not in postFields or 'username' not in postFields or 'type' not in postFields:
        return -1
      try:
        postFields.tagging = int(postFields.tagging)
        postFields.type = int(postFields.type)
      except (TypeError, ValueError):
        return -1
      if postFields.tagging <= 0 or (postFields.type != 0 and postFields.type != 1):
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tagging is owned by this user.      
      web.dbCursor.execute(u'''SELECT `userid` FROM `tags_training` WHERE `trainingid` = %s LIMIT 1''', [str(postFields.tagging)])
      if web.dbCursor.rowcount < 1:
        return -3
      taggingInfo = web.dbCursor.fetchone()
      if taggingInfo[0] != userInfo[0]:
        return -4
      # go ahead with the modification.
      try:
        web.dbCursor.execute(u'''UPDATE `tags_training` SET `type` = %s WHERE `trainingid` = %s LIMIT 1''', [str(postFields.type), str(postFields.tagging)])
      except:
        return 0
      return 1
    elif action == 'delete':
      '''
      Deletes a training tagging belonging to a user.
      Returns a standard error code.
      '''
      # validate input and authenticate user.
      if 'tagging' not in postFields or 'username' not in postFields:
        return -1
      try:
        postFields.tagging = int(postFields.tagging)
      except (TypeError, ValueError):
        return -1
      if postFields.tagging <= 0:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tagging is owned by this user.
      web.dbCursor.execute(u'''SELECT `userid` FROM `tags_training` WHERE `trainingid` = %s LIMIT 1''', [str(postFields.tagging)])
      if web.dbCursor.rowcount < 1:
        return -3
      tagInfo = web.dbCursor.fetchone()
      if tagInfo[0] != userInfo[0]:
        return -4
      # go ahead with the modification.
      try: 
        # delete the tagging itself.
        web.dbCursor.execute(u'''DELETE FROM `tags_training` WHERE `trainingid` = %s LIMIT 1''', [str(postFields.tagging)])
      except:
        return 0
      return 1
    else:
      return "Please specify a valid action."
      
class subscription:
  def checkDBConnection(self):
    if datetime.datetime.now() > web.lastQueryTime + datetime.timedelta(minutes=60):
      web.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      web.dbCursor = web.dbConn.cursor()
    web.lastQueryTime = datetime.datetime.now()
  
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    web.dbCursor.execute(u'''SELECT COUNT(*) FROM `authAttempts` WHERE `ip` = %s AND `date` >= %s''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(web.dbCursor.fetchone()[0])
    if authAttempts > 3:
      return False
    # see if this request's IP matches the last IP in our users table.
    web.dbCursor.execute(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if web.dbCursor.rowcount < 1:
      return False
    userInfo = web.dbCursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        web.dbCursor.execute(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        web.dbCursor.execute(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        return userInfo
    else:
      return userInfo
  def GET(self, action):
    getFields = web.input(name = 'web')
    if action == 'show':
      '''
      Lists all topics belonging to all of this user's subscriptions.
      '''
      # ensure that all fields are here and that user is authenticated.
      if 'username' not in getFields:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
        
      # fetch topics and tag IDs/names belonging to this user's subscriptions.
      web.dbCursor.execute(u'''SELECT `taggings`.`ll_topicid`, `taggings`.`tagid`, `tags`.`name`, `taggings`.`prob`, `tags`.`userid`, 
      (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `taggings`.`tagid`
      ) AS `subscriptionCounts` FROM `subscriptions` LEFT OUTER JOIN `taggings` ON `taggings`.`tagid` = `subscriptions`.`tagid` LEFT OUTER JOIN `tags` ON `subscriptions`.`tagid` = `tags`.`tagid` WHERE `subscriptions`.`userid` = %s ORDER BY `taggings`.`ll_topicid` DESC''', [str(userInfo[0])])
      topicCursor = web.dbConn.cursor()
      subscribedTopics = []
      addedTopicIDs = {}
      taggingInfo = web.dbCursor.fetchone()
      while taggingInfo is not None:
        if taggingInfo[0] is not None:
          if taggingInfo[0] not in addedTopicIDs:
            # set some preliminary topic info.
            topicCursor.execute(u'''SELECT `topics`.`ll_topicid`, `topics`.`userid`, `ll_users`.`username`, `topics`.`title`, 
            (
              SELECT COUNT(*) FROM `posts` WHERE `posts`.`ll_topicid` = %s
            ) AS `postCount`, 
            (
              SELECT `date` FROM `posts` WHERE `posts`.`ll_topicid` = %s ORDER BY `date` DESC LIMIT 1
            ) AS `lastPostTime` FROM `topics` LEFT OUTER JOIN `ll_users` ON `topics`.`userid` = `ll_users`.`userid` WHERE `topics`.`ll_topicid` = %s''', [str(taggingInfo[0]), str(taggingInfo[0]), str(taggingInfo[0])])
            topicInfo = topicCursor.fetchone()
            subscribedTopics.append({'id': taggingInfo[0], 'title':topicInfo[3], 'creator':{'id': topicInfo[1], 'username': topicInfo[2]}, 'postCount':topicInfo[4], 'lastPostTime':topicInfo[5], 'tags': []})
            addedTopicIDs[taggingInfo[0]] = 1
          # add this tag to the last topic.
          subscribedTopics[-1]['tags'].append({'id': taggingInfo[1], 'name': taggingInfo[2], 'prob': round(taggingInfo[3], 4), 'subscriptionCount': taggingInfo[5]})
        taggingInfo = web.dbCursor.fetchone()
      return json.dumps(subscribedTopics)
    elif action == 'list':
      '''
      Lists all subscriptions belonging to this user.
      '''
      # ensure that all fields are here and that user is authenticated.
      if 'username' not in getFields:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
        
      # fetch subscriptions belonging to this user.
      web.dbCursor.execute(u'''SELECT `subscriptions`.`subscriptionid`, `tags`.`name`, `ll_users`.`userid`, `ll_users`.`username` FROM `subscriptions` LEFT OUTER JOIN `tags` ON `subscriptions`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `ll_users` ON `tags`.`userid` = `ll_users`.`userid` WHERE `subscriptions`.`userid` = %s ORDER BY `tags`.`name` ASC''', [str(userInfo[0])])
      subscriptions = []
      subscriptionInfo = web.dbCursor.fetchone()
      while subscriptionInfo is not None:
        subscriptions.append({'id': subscriptionInfo[0], 'name': subscriptionInfo[1], 'creator':{'id': subscriptionInfo[2], 'username': subscriptionInfo[3]}})
        subscriptionInfo = web.dbCursor.fetchone()
      return json.dumps(subscriptions)
    else:
      return "Please provide a valid action."
  
  def POST(self, action):
    postFields = web.input(name = 'web')
    if action == 'create':
      '''
      Subscribes a user to a tag, if s/he has permissions to.
      Returns a standard error code.
      '''
      # check to ensure parameters present and user is authenticated.
      if 'tag' not in postFields or 'username' not in postFields:
        return -1
      try:
        postFields.tag = int(postFields.tag)
      except (TypeError, ValueError):
        # tag is not numeric.
        return -1
      if postFields.tag < 1:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      web.dbCursor.execute(u'''SELECT `userid`, `public` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      if web.dbCursor.rowcount < 1:
        return -3
      tagInfo = web.dbCursor.fetchone()
      if tagInfo[0] != userInfo[0] and tagInfo[1] == "0":
        return -4
      # otherwise, go ahead with the subscription.
      try:
        web.dbCursor.execute(u'''INSERT INTO `subscriptions` (`userid`, `tagid`) VALUES (%s, %s) ON DUPLICATE KEY UPDATE `subscriptionid` = `subscriptionid`''', [str(userInfo[0]), str(postFields.tag)])
      except:
        return 0
      return 1
    elif action == 'delete':
      '''
      Unsubscribes a user from a tag, if s/he has permissions to.
      Returns a standard error code.
      '''
      # check to ensure parameters present and user is authenticated.
      if 'tag' not in postFields or 'username' not in postFields:
        return -1
      try:
        postFields.tag = int(postFields.tag)
      except (TypeError, ValueError):
        # tag is not numeric.
        return -1
      if postFields.tag < 1:
        return -1
      # we'll need a valid DB connection, so check it now.
      self.checkDBConnection()
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # otherwise, go ahead with the unsubscription.
      try:
        web.dbCursor.execute(u'''DELETE FROM `subscriptions` WHERE `userid` = %s AND `tagid` = %s LIMIT 1''', [str(userInfo[0]), str(postFields.tag)])
      except:
        return 0
      return 1
    else:
      return "Please specify a valid action."

if __name__ == "__main__":
  try:
    # turn debug off. KEEP THIS OFF FOR PRODUCTION ENVIRONMENTS.
    web.config.debug = False
  
    # load classifier objects.
    inputFile = open('clf.txt', 'r')
    clfString = inputFile.read()
    inputFile.close()
    web.trollClassifier = [pickle.loads(clfString)]

    inputFile = open('count_vect.txt', 'r')
    countVectString = inputFile.read()
    inputFile.close()
    web.trollCountVectorizer = pickle.loads(countVectString)

    inputFile = open('tfidf_transformer.txt', 'r')
    tfIdfString = inputFile.read()
    inputFile.close()
    web.trollTfIdfTransformer = pickle.loads(tfIdfString)
    
    # Load credentials from a textfile.
    openCredentialsFile = open('credentials.txt')
    mysqlLogin = openCredentialsFile.readline().strip('\n').split(',')
    if len(mysqlLogin) < 2:
      print "MySQL login not found in credentials file."
      exit()

    web.MYSQL_USERNAME = mysqlLogin[0]
    web.MYSQL_PASSWORD = mysqlLogin[1]
    mysqlDatabases = openCredentialsFile.readline().strip('\n').split(',')
    if len(mysqlDatabases) < 1:
      print "Database data not found in credentials file."
      exit()
    web.MYSQL_DATABASE = mysqlDatabases[0]
    openCredentialsFile.close()
    
    # create database objects.
    web.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
    web.dbCursor = web.dbConn.cursor()
    web.lastQueryTime = datetime.datetime.now()
    
    # load all public tags into memory.
    web.dbCursor.execute(u'''SELECT `tags`.`tagid`, `tags`.`name`, `tags`.`classifier`, `tags`.`countVectorizer`, `tags`.`tfidfTransformer`, 
      (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` WHERE (`tags`.`public` = 1 AND LENGTH(`tags`.`classifier`) > 0) ORDER BY `subscriptionCount` DESC''')
    web.publicTags = {}
    tag = web.dbCursor.fetchone()
    while tag is not None:
      try:
        web.publicTags[tag[0]] = (tag[1], pickle.loads(tag[2]), pickle.loads(tag[3]), pickle.loads(tag[4]), int(tag[5]))
      except EOFError:
        # missing a classifier / count vectorizer / tfidf transformer field. skip it.
        tag = web.dbCursor.fetchone()
        continue
      tag = web.dbCursor.fetchone()

  except:
    print "Could not instantiate classifier or database objects."
    sys.exit(1)
  app.run()