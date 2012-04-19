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

def db_load_hook():
    web.ctx.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)

def db_unload_hook():
    web.ctx.dbConn.close()

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
  def queryDB(self, query, args):
    try:
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
    except MySQLdb.OperationalError:
      # lost connect. reconnect and re-query.
      web.ctx.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    cursor = self.queryDB(u'''SELECT COUNT(*) FROM `authAttempts` WHERE (`ip` = %s && `date` >= %s && `type` = 0)''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(cursor.fetchone()[0])
    if authAttempts > 3:
      return False
    
    # see if this request's IP matches the last IP in our users table.
    cursor = self.queryDB(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if cursor.rowcount < 1:
      return False
    if cursor.rowcount < 1:
      return False
    userInfo = cursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        cursor = self.queryDB(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        cursor = self.queryDB(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
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
        
      if 'username' in getFields:
        # user is requesting just the tags s/he is subscribed to.
        # first, authenticate this user.
        userInfo = self.authenticateUser(getFields.username)
        if not userInfo:
          return -2
        # now load the relevant tags.
        cursor = self.queryDB(u'''SELECT `subscriptions`.`tagid`, `tags`.`name`, `tags`.`classifier`, `tags`.`countVectorizer`, `tags`.`tfidfTransformer`, 
          (
            SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
          ) AS `subscriptionCount`, `tags`.`userid`, `ll_users`.`username` FROM `subscriptions` LEFT OUTER JOIN `tags` ON `subscriptions`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `tags`.`userid` WHERE (`subscriptions`.`userid` = %s AND LENGTH(`tags`.`classifier`) > 0) ORDER BY `subscriptionCount` DESC''', [str(userInfo[0])])
        relevantTags = {}
        tag = cursor.fetchone()
        while tag is not None:
          try:
            relevantTags[tag[0]] = (tag[1], pickle.loads(tag[2]), pickle.loads(tag[3]), pickle.loads(tag[4]), int(tag[5]), int(tag[6]), str(tag[7]))
          except EOFError:
            # this tag is missing a classifier / count vectorizer / tfidf transformer field. skip it.
            tag = cursor.fetchone()
            continue
          tag = cursor.fetchone()
      else:
        # user is requesting all public tags.
        relevantTags = web.publicTags
        userInfo = (0, '')
        
      returnList = []
      for topicID in topics:
        topicDict = {'id': topicID}
        tagList = []
        # get topic title and OP.
        cursor = self.queryDB(u'''SELECT `topics`.`title`, `posts`.`messagetext` FROM `topics` LEFT OUTER JOIN `posts` ON `topics`.`ll_topicid` = `posts`.`ll_topicid` WHERE `topics`.`ll_topicid` = %s ORDER BY `posts`.`date` ASC LIMIT 1''', [str(topicID)])
        topicInfo = cursor.fetchone()
        if topicInfo is not None:
          topicString = " ".join([unicode(text) for text in topicInfo])
          for tagID in relevantTags:
            transformedText = etiClassifier.transformData([etiClassifier.stripPostHTML(topicString)], count_vect=relevantTags[tagID][2], tfidf_transformer=relevantTags[tagID][3])[0]
            tagList.append({'id': tagID, 'name': relevantTags[tagID][0], 'prob': "%.4f" % round(relevantTags[tagID][1][0].predict_proba(transformedText)[0], 4), 'creator': {'id': int(relevantTags[tagID][5]), 'username': str(relevantTags[tagID][6])}, 'owned': bool(relevantTags[tagID][5] == userInfo[0]), 'subscriptionCount': relevantTags[tagID][4]})
          
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
      
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2

      # user is authenticated. proceed with classification submission.
      # see if this tag already exists in the table.
      cursor = self.queryDB(u'''SELECT `tagid`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` WHERE `userid` = %s AND `name` = %s LIMIT 1''', [str(userInfo[0]), str(postFields.tag)])
      if cursor.rowcount < 1:
        # new tag. insert and set tagid to be this last-inserted ID.
        cursor = self.queryDB(u'''INSERT INTO `tags` (`name`, `userid`, `classifier`, `countVectorizer`, `tfidfTransformer`) VALUES (%s, %s, '', '', '')''', [str(postFields.tag), str(userInfo[0])])
        tagInfo = (web.ctx.dbConn.insert_id(), 1)
        # subscribe this user to this tag as well.
        cursor = self.queryDB(u'''INSERT INTO `subscriptions` (`userid`, `tagid`) VALUES (%s, %s)''', [str(userInfo[0]), str(tagInfo[0])])
      else:
        # pre-existing tag. set tagid to be this ID.
        tagInfo = cursor.fetchone()
      # insert this training entry.
      cursor = self.queryDB(u'''INSERT INTO `tags_training` (`tagid`, `topicid`, `type`, `userid`) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE `type` = %s''', [str(tagInfo[0]), str(postFields.topic), str(postFields.type), str(userInfo[0]), str(postFields.type)])
      # update classifier with new data.
      (X, y) = etiClassifier.getTopicData(tagInfo[0], username=web.MYSQL_USERNAME, password=web.MYSQL_PASSWORD, database=web.MYSQL_DATABASE)
      if len(X) > 1 and 0 in y and 1 in y:
        etiClassifier.shuffle_in_unison(X, y)
        (X_train_tfidf,count_vect,tfidf_transformer) = etiClassifier.transformData(X)
        (classifier) = etiClassifier.trainClassifiers(X_train_tfidf, y, classifiers=["l2log"])
        cursor = self.queryDB(u'''UPDATE `tags` SET `classifier` = %s, `countVectorizer` = %s, `tfidfTransformer` = %s WHERE `tagid` = %s LIMIT 1''', [pickle.dumps(classifier), pickle.dumps(count_vect), pickle.dumps(tfidf_transformer), str(tagInfo[0])])
      
      # insert or remove a tagging for this topic and this tag.
      if postFields.type == 0:
        cursor = self.queryDB(u'''DELETE FROM `taggings` WHERE `tagid` = %s AND `ll_topicid` = %s LIMIT 1''', [str(tagInfo[0]), str(postFields.topic)])
      else:
        cursor = self.queryDB(u'''INSERT INTO `taggings` (`tagid`, `ll_topicid`, `prob`) VALUES (%s, %s, 1) ON DUPLICATE KEY UPDATE `prob` = 1''', [str(tagInfo[0]), str(postFields.topic)])
      return json.dumps({'id': tagInfo[0], 'name': postFields.tag, 'subscriptionCount': tagInfo[1]})
    else:
      return "Please specify a valid action."
    
class tag:
  def queryDB(self, query, args):
    try:
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
    except MySQLdb.OperationalError:
      # lost connect. reconnect and re-query.
      web.ctx.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    cursor = self.queryDB(u'''SELECT COUNT(*) FROM `authAttempts` WHERE (`ip` = %s && `date` >= %s && `type` = 0)''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(cursor.fetchone()[0])
    if authAttempts > 3:
      return False
    
    # see if this request's IP matches the last IP in our users table.
    cursor = self.queryDB(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if cursor.rowcount < 1:
      return False
    if cursor.rowcount < 1:
      return False
    userInfo = cursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        cursor = self.queryDB(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        cursor = self.queryDB(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        return userInfo
    else:
      return userInfo
  def GET(self, action):
    getFields = web.input(name = 'web')
    if action == 'show':
      '''
      Displays a tag's information.
      '''
      if 'tag' not in getFields or 'username' not in getFields:
        return -1
      try:
        getFields.tag = int(getFields.tag)
      except ValueError, TypeError:
        return -1
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      cursor = self.queryDB(u'''SELECT `tags`.`tagid`, `tags`.`name`, `tags`.`userid`, `tags`.`public`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(getFields.tag)])
      if cursor.rowcount < 1:
        return -3
      tagInfo = cursor.fetchone()
      if tagInfo[0] != userInfo[0] and tagInfo[1] == "0":
        return -4
      # otherwise, go ahead with the display.
      tagInfo = cursor.fetchone()
      return json.dumps({'id': int(tagInfo[0]), 'name': str(tagInfo[1]), 'subscriptionCount': int(tagInfo[4]), 'owned': bool(userInfo[0] == tagInfo[2])})
      
    elif action == 'topics':
      '''
      Displays a list of topics belonging to a tag.
      '''
      # check to ensure that request is well-formed and that user is authenticated to view this tag.
      if 'tag' not in getFields or 'username' not in getFields:
        return -1
      try:
        getFields.tag = int(getFields.tag)
      except ValueError, TypeError:
        return -1
      if 'page' not in getFields:
        page = 1
      else:
        try:
          page = int(getFields.page)
        except TypeError, ValueError:
          return -1
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      cursor = self.queryDB(u'''SELECT `tags`.`tagid`, `tags`.`name`, `ll_users`.`username`, `tags`.`userid`, `tags`.`public`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = %s
      ) AS `subscriptionCount` FROM `tags` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `tags`.`userid` WHERE `tagid` = %s LIMIT 1''', [str(getFields.tag),str(getFields.tag)])
      if cursor.rowcount < 1:
        return -3
      tagInfo = cursor.fetchone()
      if tagInfo[3] != userInfo[0] and tagInfo[4] == "0":
        return -4
      # otherwise, go ahead with the display.
      cursor = self.queryDB(u'''SELECT COUNT(*) FROM `taggings` WHERE `tagid` = %s''', [str(getFields.tag)])
      numTopics = int(cursor.fetchone()[0])
      taggedTopics = []
      if numTopics > 0:
        cursor = self.queryDB(u'''SELECT `topics`.`ll_topicid`, `topics`.`boardid`, `topics`.`title`, `topics`.`userid`, `ll_users`.`username`, `topics`.`postCount`, `topics`.`lastPostTime` FROM `topics` LEFT OUTER JOIN `taggings` ON `taggings`.`ll_topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` ON `topics`.`userid` = `ll_users`.`userid` WHERE `taggings`.`tagid` = %s ORDER BY `topics`.`lastPostTime` DESC LIMIT ''' + str((page-1)*50) + u''', 50''', [str(getFields.tag)])
        taggingCursor = web.ctx.dbConn.cursor()
        topicInfo = cursor.fetchone()
        while topicInfo is not None:
          # get this topic's tag info.
          taggingCursor.execute(u'''SELECT `taggings`.`tagid`, `tags`.`name`, `taggings`.`prob`, `tags`.`userid`, `ll_users`.`username` FROM `taggings` LEFT OUTER JOIN `tags` ON `tags`.`tagid` = `taggings`.`tagid` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `tags`.`userid` WHERE `taggings`.`ll_topicid` = %s''', [str(topicInfo[0])])
          topicTags = [{'id': taggingInfo[0], 'name': taggingInfo[1], 'prob': "%.4f" % taggingInfo[2], 'creator': {'id': int(taggingInfo[3]), 'username': str(taggingInfo[4])}, 'owned': bool(userInfo[0] == taggingInfo[3])} for taggingInfo in taggingCursor.fetchall()]
          taggedTopics.append({'id': topicInfo[0], 'board': topicInfo[1], 'title': topicInfo[2], 'creator': {'id': topicInfo[3], 'username': topicInfo[4]}, 'postCount': topicInfo[5], 'lastPostTime': topicInfo[6], 'tags': topicTags})
          topicInfo = cursor.fetchone()        
      return json.dumps({'tag': {'id': int(tagInfo[0]), 'name': str(tagInfo[1]), 'subscriptionCount': int(tagInfo[5]), 'creator': {'id': int(tagInfo[3]), 'username': str(tagInfo[2])}, 'owned': bool(tagInfo[3] == userInfo[0])}, 'pages': int(math.ceil(numTopics/50.0)), 'topics': taggedTopics})
    elif action == 'owned':
      '''
      Takes a user's name and returns a JSON list of tag objects belonging to this user.
      '''
      # validate input fields and user auth.
      if 'username' not in getFields:
        return -1
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # otherwise go ahead and list these tags.
      cursor = self.queryDB(u'''SELECT `tags`.`tagid`, `tags`.`name`, `tags`.`public`, `tags`.`userid`, `ll_users`.`username`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `tags`.`userid` WHERE `tags`.`userid` = %s''', [str(userInfo[0])])
      ownedTags = [{'id': tagInfo[0], 'name': tagInfo[1], 'public': bool(int(tagInfo[2])), 'creator': {'id': int(tagInfo[3]), 'username': str(tagInfo[4])}, 'subscriptionCount': int(tagInfo[5])} for tagInfo in cursor.fetchall()]
      return json.dumps(ownedTags)
    
    elif action == 'search':
      '''
      Performs a search amongst public tags.
      Returns a JSON list of tag objects.
      '''
      # authenticate user.
      if 'username' not in getFields:
        return -1
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      if 'page' not in getFields:
        page = 1
      else:
        try:
          page = int(getFields.page)
        except TypeError, ValueError:
          return -1
      
      if 'q' in getFields:
        # search for this particular tag name.
        searchTerms = getFields.q.split()        
        cursor = self.queryDB(u'''SELECT COUNT(*) FROM `tags` WHERE (`tags`.`public` = 1 OR `tags`.`userid` = %s) AND MATCH(`tags`.`name`) AGAINST(%s IN BOOLEAN MODE)''', [str(userInfo[0]), str(getFields.q)])
        numTags = int(cursor.fetchone()[0])
        if numTags > 0:
          cursor = self.queryDB(u'''SELECT `tags`.`tagid`, `tags`.`name`, (
              SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
            ) AS `subscriptionCount`, `tags`.`userid`, `ll_users`.`username` FROM `tags` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `tags`.`userid` WHERE (`tags`.`public` = 1 OR `tags`.`userid` = %s) AND MATCH(`tags`.`name`) AGAINST(%s IN BOOLEAN MODE) ORDER BY `subscriptionCount` DESC LIMIT ''' + str((page-1)*50) + u''', 50''', [str(userInfo[0]), str(getFields.q)])
          return json.dumps({'pages': int(math.ceil(numTags/50.0)), 'tags': [{'id': tag[0], 'name':tag[1], 'subscriptionCount':tag[2], 'creator': {'id': int(tag[3]), 'username': str(tag[4])}, 'owned': bool(tag[3] == userInfo[0])} for tag in cursor.fetchall()]})
        else:
          return json.dumps({'pages': 0, 'tags': []})
      else:
        # just get all public tags, which we conveniently have in memory.
        return json.dumps({'pages': int(math.ceil(len(web.publicTags)/50.0)), 'tags': [{'id': tag, 'name':web.publicTags[tag][0], 'subscriptionCount':web.publicTags[tag][4], 'creator': {'id': int(web.publicTags[tag][5]), 'username': str(web.publicTags[tag][6])}, 'owned': bool(web.publicTags[tag][5] == userInfo[0])} for tag in web.publicTags]})
        
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
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      cursor = self.queryDB(u'''SELECT `tags`.`userid`, `tags`.`public`, `tags`.`name`, `tags`.`classifier`, `tags`.`countVectorizer`, `tags`.`tfidfTransformer`, (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      if cursor.rowcount < 1:
        return -3
      tagInfo = cursor.fetchone()
      if tagInfo[0] != userInfo[0] and tagInfo[1] == "0":
        return -4
      # go ahead with the modification.
      try:
        cursor = self.queryDB(u'''UPDATE `tags` SET `public` = %s WHERE `tagid` = %s LIMIT 1''', [str(postFields.public), str(postFields.tag)])
        cursor = self.queryDB(u'''SELECT COUNT(*) FROM `tags_training` WHERE `tagid` = %s''', [str(postFields.tag)])
        numTrainingData = int(cursor.fetchone()[0])
        if postFields.public is 1 and numTrainingData >= web.minTrainingForFeeds and str(postFields.tag) not in web.publicTags:
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
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user.
      cursor = self.queryDB(u'''SELECT `userid`, `public` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      if cursor.rowcount < 1:
        return -3
      tagInfo = cursor.fetchone()
      if tagInfo[0] != userInfo[0]:
        return -4
      # go ahead with the modification.
      try:
        # first, delete all the subscriptions belonging to this tag.
        cursor = self.queryDB(u'''DELETE FROM `subscriptions` WHERE `tagid` = %s''', [str(postFields.tag)])
        
        # second, delete all taggings belonging to this tag.
        cursor = self.queryDB(u'''DELETE FROM `taggings` WHERE `tagid` = %s''', [str(postFields.tag)])
        
        # third, delete all the training data belonging to this tag.
        cursor = self.queryDB(u'''DELETE FROM `tags_training` WHERE `tagid` = %s''', [str(postFields.tag)])
        
        # fourth, remove this tag from publicTags (if it needs to be removed).
        if tagInfo[1] is "1":
          del(web.publicTags[str(postFields.tag)])        
        # finally, delete the tag itself.
        self.queryDB(u'''DELETE FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      except:
        return 0
      return 1
      
    else:
      return "Please specify a valid action."

class tagging:
  def queryDB(self, query, args):
    try:
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
    except MySQLdb.OperationalError:
      # lost connect. reconnect and re-query.
      web.ctx.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    cursor = self.queryDB(u'''SELECT COUNT(*) FROM `authAttempts` WHERE (`ip` = %s && `date` >= %s && `type` = 0)''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(cursor.fetchone()[0])
    if authAttempts > 3:
      return False
    
    # see if this request's IP matches the last IP in our users table.
    cursor = self.queryDB(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if cursor.rowcount < 1:
      return False
    if cursor.rowcount < 1:
      return False
    userInfo = cursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        cursor = self.queryDB(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        
        return False
      else:
        # this lines up. update the IP in our table.
        cursor = self.queryDB(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        
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
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
      # get a list of taggings for this user (and if topic is provided, restrict to just that topic.)
      if 'topic' in getFields:
        try:
          getFields.topic = int(getFields.topic)
        except (TypeError, ValueError):
          return -1
        cursor = self.queryDB(u'''SELECT `tags_training`.`trainingid`, `tags_training`.`tagid`, `tags`.`name`, `tags_training`.`topicid`, `topics`.`title`, `topics`.`userid`, `ll_users`.`username`, `tags_training`.`type` FROM `tags_training` LEFT OUTER JOIN `tags` ON `tags_training`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `topics` ON `tags_training`.`topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `topics`.`userid` WHERE (`tags_training`.`userid` = %s AND `tags_training`.`topicid` = %s)''', [str(userInfo[0]), str(getFields.topic)])
      else:
        cursor = self.queryDB(u'''SELECT `tags_training`.`trainingid`, `tags_training`.`tagid`, `tags`.`name`, `tags_training`.`topicid`, `topics`.`title`, `topics`.`userid`, `ll_users`.`username`, `tags_training`.`type` FROM `tags_training` LEFT OUTER JOIN `tags` ON `tags_training`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `topics` ON `tags_training`.`topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `topics`.`userid` WHERE `tags_training`.`userid` = %s''', [str(userInfo[0])])
      trainingList = []
      trainingInfo = cursor.fetchone()
      while trainingInfo is not None:
        trainingList.append({'id': int(trainingInfo[0]), 'tag':{'id': int(trainingInfo[1]), 'name': trainingInfo[2]}, 'topic':{'id': int(trainingInfo[3]), 'title': trainingInfo[4], 'creator': {'id': int(trainingInfo[5]), 'username': trainingInfo[6]}}, 'type': int(trainingInfo[7])})
        trainingInfo = cursor.fetchone()
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
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tagging is owned by this user.      
      cursor = self.queryDB(u'''SELECT `userid` FROM `tags_training` WHERE `trainingid` = %s LIMIT 1''', [str(postFields.tagging)])
      if cursor.rowcount < 1:
        return -3
      taggingInfo = cursor.fetchone()
      if taggingInfo[0] != userInfo[0]:
        return -4
      # go ahead with the modification.
      try:
        cursor = self.queryDB(u'''UPDATE `tags_training` SET `type` = %s WHERE `trainingid` = %s LIMIT 1''', [str(postFields.type), str(postFields.tagging)])
        
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
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tagging is owned by this user.
      cursor = self.queryDB(u'''SELECT `userid` FROM `tags_training` WHERE `trainingid` = %s LIMIT 1''', [str(postFields.tagging)])
      if cursor.rowcount < 1:
        return -3
      tagInfo = cursor.fetchone()
      if tagInfo[0] != userInfo[0]:
        return -4
      # go ahead with the modification.
      try: 
        # delete the tagging itself.
        cursor = self.queryDB(u'''DELETE FROM `tags_training` WHERE `trainingid` = %s LIMIT 1''', [str(postFields.tagging)])
        
      except:
        return 0
      return 1
    else:
      return "Please specify a valid action."
      
class subscription:
  def queryDB(self, query, args):
    try:
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
    except MySQLdb.OperationalError:
      # lost connect. reconnect and re-query.
      web.ctx.dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
      cursor = web.ctx.dbConn.cursor()
      cursor.execute(query, args)
      web.ctx.dbConn.commit()
      return cursor
  def authenticateUser(self, username):
    # first, see if this request needs to be rejected.
    attemptCutoff = datetime.datetime.now() - datetime.timedelta(minutes=10)
    cursor = self.queryDB(u'''SELECT COUNT(*) FROM `authAttempts` WHERE (`ip` = %s && `date` >= %s && `type` = 0)''', [str(web.ctx['ip']), str(time.mktime(attemptCutoff.timetuple()))])
    authAttempts = int(cursor.fetchone()[0])
    if authAttempts > 3:
      return False
    
    # see if this request's IP matches the last IP in our users table.
    cursor = self.queryDB(u'''SELECT `userid`, `last_ip` FROM `ll_users` WHERE `username` = %s LIMIT 1''', [str(username)])
    if cursor.rowcount < 1:
      return False
    if cursor.rowcount < 1:
      return False
    userInfo = cursor.fetchone()
    if userInfo[1] != web.ctx['ip']:
      # check LL to see if this IP lines up.
      validateIP = urllib2.urlopen('http://boards.endoftheinter.net/scripts/login.php?username=' + str(username) + '&ip=' + str(web.ctx['ip']))
      validateIP = validateIP.read()
      if validateIP != "1:" + username:
        # this doesn't line up. return auth error and log attempt.
        attemptTime = time.mktime(datetime.datetime.now().timetuple())
        cursor = self.queryDB(u'''INSERT INTO `authAttempts` (`date`, `userid`, `ip`, `type`) VALUES (%s, %s, %s, %s)''', [str(attemptTime), str(userInfo[0]), str(web.ctx['ip']), str(0)])
        return False
      else:
        # this lines up. update the IP in our table.
        cursor = self.queryDB(u'''UPDATE `ll_users` SET `last_ip` = %s WHERE `userid` = %s LIMIT 1''', [str(web.ctx['ip']), str(userInfo[0])])
        return userInfo
    else:
      return userInfo
  def GET(self, action):
    getFields = web.input(name = 'web')
    if action == 'topics':
      '''
      Lists all topics belonging to all of this user's subscriptions.
      '''
      # ensure that all fields are here and that user is authenticated.
      if 'username' not in getFields:
        return -1
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
        
      if 'prob' not in getFields:
        prob = 0.5
      else:
        try:
          prob = float(getFields.prob)
        except TypeError, ValueError:
          return -1
        
      if 'page' not in getFields:
        page = 1
      else:
        try:
          page = int(getFields.page)
        except TypeError, ValueError:
          return -1
      # first, fetch number of taggings in this user's feed.
      cursor = self.queryDB(u'''SELECT COUNT(*) FROM `subscriptions` LEFT OUTER JOIN `taggings` ON `taggings`.`tagid` = `subscriptions`.`tagid` WHERE `subscriptions`.`userid` = %s''', [str(userInfo[0])])
      taggingCount = int(cursor.fetchone()[0])
      if taggingCount > 0:
        # fetch topics and tag IDs/names belonging to this user's subscriptions.
        cursor = self.queryDB(u'''SELECT `taggings`.`ll_topicid`, `taggings`.`tagid`, `tags`.`name`, `taggings`.`prob`, `tags`.`userid`, `tagCreators`.`username` AS `tagCreator`, 
        (
          SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `taggings`.`tagid`
        ) AS `subscriptionCounts`, `topics`.`boardid`, `topics`.`userid`, `topicCreators`.`username` AS `topicCreator`, `topics`.`title`, `topics`.`postCount`, `topics`.`lastPostTime` FROM `subscriptions` LEFT OUTER JOIN `taggings` ON `taggings`.`tagid` = `subscriptions`.`tagid` LEFT OUTER JOIN `tags` ON `subscriptions`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `ll_users` AS `tagCreators` ON `tags`.`userid` = `tagCreators`.`userid` LEFT OUTER JOIN `topics` ON `taggings`.`ll_topicid` = `topics`.`ll_topicid` LEFT OUTER JOIN `ll_users` AS `topicCreators` ON `topicCreators`.`userid` = `topics`.`userid` WHERE (`subscriptions`.`userid` = %s AND `taggings`.`prob` >= %s) ORDER BY `topics`.`lastPostTime` DESC LIMIT ''' + str((page-1)*50) + u''', 50''', [str(userInfo[0]), str(prob)])
        subscribedTopics = []
        addedTopicIDs = {}
        taggingInfo = cursor.fetchone()
        while taggingInfo is not None:
          if taggingInfo[0] is not None:
            if taggingInfo[0] not in addedTopicIDs:
              # set some preliminary topic info.
              subscribedTopics.append({'id': taggingInfo[0], 'board': taggingInfo[7], 'title':taggingInfo[10], 'creator':{'id': taggingInfo[8], 'username': taggingInfo[9]}, 'postCount':taggingInfo[11], 'lastPostTime':taggingInfo[12], 'tags': []})
              addedTopicIDs[taggingInfo[0]] = 1
            # add this tag to the last topic.
            subscribedTopics[-1]['tags'].append({'id': taggingInfo[1], 'name': taggingInfo[2], 'creator': {'id': taggingInfo[4], 'username': taggingInfo[5]}, 'prob': "%.4f" % round(taggingInfo[3], 4), 'subscriptionCount': taggingInfo[6]})

          taggingInfo = cursor.fetchone()
        return json.dumps({'pages': int(math.ceil(taggingCount/50.0)), 'topics':subscribedTopics})
    elif action == 'list':
      '''
      Lists all subscriptions belonging to this user.
      '''
      # ensure that all fields are here and that user is authenticated.
      if 'username' not in getFields:
        return -1
      userInfo = self.authenticateUser(getFields.username)
      if not userInfo:
        return -2
        
      # fetch subscriptions belonging to this user.
      cursor = self.queryDB(u'''SELECT `subscriptions`.`subscriptionid`, `subscriptions`.`tagid`, `tags`.`name`, `ll_users`.`userid`, `ll_users`.`username`, (
        SELECT COUNT(*) FROM `subscriptions` AS `subscriptions2` WHERE `subscriptions2`.`tagid` = `subscriptions`.`tagid`
      ) AS `subscriptionCount` FROM `subscriptions` LEFT OUTER JOIN `tags` ON `subscriptions`.`tagid` = `tags`.`tagid` LEFT OUTER JOIN `ll_users` ON `tags`.`userid` = `ll_users`.`userid` WHERE `subscriptions`.`userid` = %s ORDER BY `tags`.`name` ASC''', [str(userInfo[0])])
      subscriptions = []
      subscriptionInfo = cursor.fetchone()
      while subscriptionInfo is not None:
        tagObject = {'id': int(subscriptionInfo[1]), 'name': subscriptionInfo[2], 'creator':{'id': int(subscriptionInfo[3]), 'username': subscriptionInfo[4]}, 'subscriptionCount': int(subscriptionInfo[5]), 'owned': bool(subscriptionInfo[3] == userInfo[0])}
        subscriptions.append({'id': int(subscriptionInfo[0]), 'tag': tagObject})
        subscriptionInfo = cursor.fetchone()
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
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # check to make sure this tag is owned by this user or that it's public.
      cursor = self.queryDB(u'''SELECT `userid`, `public` FROM `tags` WHERE `tagid` = %s LIMIT 1''', [str(postFields.tag)])
      if cursor.rowcount < 1:
        return -3
      tagInfo = cursor.fetchone()
      if tagInfo[0] != userInfo[0] and tagInfo[1] == "0":
        return -4
      # otherwise, go ahead with the subscription.
      try:
        cursor = self.queryDB(u'''SELECT COUNT(*) FROM `subscriptions` WHERE (`userid` = %s && `tagid` = &s) LIMIT 1''', [str(userInfo[0]), str(postFields.tag)])
        numSubs = int(cursor.fetchone()[0])
        if numSubs < 1:
          if tag in web.publicTags:
            web.publicTags[tag][4] += 1
          cursor = self.queryDB(u'''INSERT INTO `subscriptions` (`userid`, `tagid`) VALUES (%s, %s) ON DUPLICATE KEY UPDATE `subscriptionid` = `subscriptionid`''', [str(userInfo[0]), str(postFields.tag)])
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
      userInfo = self.authenticateUser(postFields.username)
      if not userInfo:
        return -2
      # otherwise, go ahead with the unsubscription.
      try:
        cursor = self.queryDB(u'''SELECT COUNT(*) FROM `subscriptions` WHERE (`userid` = %s && `tagid` = &s) LIMIT 1''', [str(userInfo[0]), str(postFields.tag)])
        numSubs = int(cursor.fetchone()[0])
        if numSubs > 0:
          if tag in web.publicTags:
            web.publicTags[tag][4] -= 1
          cursor = self.queryDB(u'''DELETE FROM `subscriptions` WHERE `userid` = %s AND `tagid` = %s LIMIT 1''', [str(userInfo[0]), str(postFields.tag)])
      except:
        return 0
      return 1
    else:
      return "Please specify a valid action."

if __name__ == "__main__":
  try:
    # turn debug off. KEEP THIS OFF FOR PRODUCTION ENVIRONMENTS.
    web.config.debug = False
    web.minTrainingForFeeds = 50
  
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
    
    # create database connection.
    app.add_processor(web.loadhook(db_load_hook))
    app.add_processor(web.unloadhook(db_unload_hook))
    
    dbConn = MySQLdb.connect('localhost', web.MYSQL_USERNAME, web.MYSQL_PASSWORD, web.MYSQL_DATABASE, use_unicode=True)
    cursor = dbConn.cursor()
    
    # load all public tags into memory.
    cursor.execute(u'''SELECT `tags`.`tagid`, `tags`.`name`, `tags`.`classifier`, `tags`.`countVectorizer`, `tags`.`tfidfTransformer`, 
      (
        SELECT COUNT(*) FROM `subscriptions` WHERE `subscriptions`.`tagid` = `tags`.`tagid`
      ) AS `subscriptionCount`, `tags`.`userid`, `ll_users`.`username` FROM `tags` LEFT OUTER JOIN `ll_users` ON `ll_users`.`userid` = `tags`.`userid`
      WHERE (`tags`.`public` = 1 AND LENGTH(`tags`.`classifier`) > 0) ORDER BY `subscriptionCount` DESC''')
    web.publicTags = {}
    tag = cursor.fetchone()
    while tag is not None:
      try:
        web.publicTags[tag[0]] = (tag[1], pickle.loads(tag[2]), pickle.loads(tag[3]), pickle.loads(tag[4]), int(tag[5]), int(tag[6]), str(tag[7]))
      except EOFError:
        # missing a classifier / count vectorizer / tfidf transformer field. skip it.
        tag = cursor.fetchone()
        continue
      tag = cursor.fetchone()

  except MySQLdb.OperationalError:
    print "Could not instantiate classifier or database objects."
    sys.exit(1)
  app.run()