#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''
Created on Jan 8, 2013

@author: daniel
'''

import itertools
import json
import re
import atexit
import sys
import time
from datetime import datetime
from xml.etree import ElementTree as ET
from code import interact
from threading import Thread
from time import sleep
from xml.dom.minidom import Document
from uuid import uuid1
from collections import deque, OrderedDict
from base64 import encodestring
from overrides.http import MyHTTP, MyHTTPS


async = 'com.broadsoft.async'
actions = 'com.broadsoft.xsi-actions'
events = 'com.broadsoft.xsi-events'
schema = 'http://schema.broadsoft.com/xsi'
w3 = 'http://www.w3.org/2001/XMLSchema-instance'
version = 'v2.0'
proto = re.compile('^https?://')

# Set these values for your XSI Server / Host.
baseurl = 'my.xsi-server.com'
user = 'user@xsi-server.com'
pwd = '>myxsipwd<'
enterprise = '>myenterprise<'


def debug(*args):
  if not hasattr(sys, 'frozen'):
    print datetime.now(), args


class EventsChannel(object):
  """
    An Events Channel must track an HTTP Connection, Response(es),
    an ID received during establish, and be capable of processing
    modification and shutdown events.  It also needs to issue
    event responses and implement a heartbeat to keep the HTTPS
    connection open.
  """
  headers = {
   'Content-Type': 'application/xml',
   'Accept': 'application/json',
   'Authorization': 'Basic %s' % encodestring('%s:%s' % (user, pwd)).strip()
  }

  def __init__(self, conduit = True, conduits = 5, expire = 3600):
    self.csid = str(uuid1())
    self.numconduits = conduits
    self.expconduits = expire
    self.conduits = OrderedDict()
    self.closing = False
    self.data = deque()
    self.processed = deque()
    self.errors = deque(maxlen = 10)
    self.events = set()
    self.subscriptions = {}
    self.sequences = {}
    if conduit:
      self.channeldaemon(start = True)
      self.processordaemon(start = True)

  def channeldaemon(self, start = False):
    if start:
      t = Thread(target = self.channeldaemon, name = 'Channel Daemon')
      t.daemon = True
      t.start()
      return atexit.register(self.closechannels)
    mark = time.time()
    while not self.closing:
      # Every 15 minutes tear down the oldest channel.
      if time.time() - mark > 600:
        self.verifychannelset()
        mark = time.time()
        cid = self.conduits.keys()[0]
        r = self.closechannel(cid)
        if r.status == 403:
          # Channel missing - discard so it will regenerate.
          self.conduits.pop(cid, None)
      if len(self.conduits) < self.numconduits:
        if len(self.conduits) < 1:
          self.make_conduit()
        self.make_conduit()
      sleep(25)
      self.heartbeat()
      # The channel daemon may as well keep our subscriptions alive.
      self.rxrenew()

  def verifychannelset(self):
    J = json.loads(self.getchannelset().read())
    if 'ErrorInfo' in J and J['ErrorInfo']['errorCode']['$'] == '110842':
      self.newchannelset()
    debug(J)

  def newchannelset(self):
    self.csid = str(uuid1())
    for cid in self.conduits.keys():
      self.closechannel(cid)
      self.conduits.pop(cid, None)
    # Setup two initial channels, let Daemon bring the rest back online.
    for _ in range(2):
      self.make_conduit()
    sleep(3)
    # After we generate a new channel set we need to recreate subscriptions.
    self.newsubscriptions()

  def newsubscriptions(self):
    self.subscriptions.clear()
    for event in self.events:
      self.enterprise_subscribe(event)

  def closechannel(self, cid):
    return self.delete(self.url(events, version, 'channel', cid))

  def rxrenew(self):
    for rxid, (created, lifetime, event) in self.subscriptions.items():
      # Close subscriptions 5 minutes before expiration.
      # They should be regenerated if we're not closing.
      try:
        RX = json.loads(self.getsubscription(rxid).read())
      except:
        # No connection?  Check again later.
        continue
      if 'ErrorInfo' in RX and RX['ErrorInfo']['errorCode']['$'] == '110862':
        defunct = True
      else:
        defunct = False
      if defunct or time.time() - created > lifetime - 300:  # 3570 - 30 seconds.
        R = self.unsubscribe(rxid)
        if R.status == 200:
          continue
        if R.status == 403:
          error = json.loads(R.read())
          debug(error)
          if error['ErrorInfo']['errorCode']['$'] == '110862':
            # Our subscription isn't listed (?).  Figure out why at some point.
            # Resubscribe manually for now.
            debug('Subscription missing - ', error, R.status)
            self.subscriptions.pop(rxid, None)
            self.enterprise_subscribe(event)
            continue
        debug("Unknown Unsubscribe Error -", rxid, R.status, R.read())

  def closechannels(self):
    self.closing = True
    for cid in self.conduits.keys():
      R = self.closechannel(cid)
      if R.status != 200:
        debug("Unable to close channel '%s'" % cid)

  def heartbeat(self):
    for cid in self.conduits.keys():
      try:
        self.put(self.url(events, version, 'channel', cid, 'heartbeat'))
      except Exception, e:
        debug(cid, e)

  def reader(self, cid):
    while cid in self.conduits:
      data = self.conduits[cid].read()
      if data:
        self.data.append(data)
      sleep(.25)
    debug("Channel closed - ", cid)

  def processordaemon(self, start = False):
    if start:
      T = Thread(target = self.processordaemon, name = 'Processor Daemon')
      T.daemon = True
      return T.start()
    while self.conduits or not self.closing:
      if not self.data:
        sleep(.25)
        continue
      chunk = self.data.popleft()
      self.handlexml(chunk)

  def handlexml(self, xmlstring):
    xml = ET.fromstring(xmlstring)
    # We can safely ignore channel heartbeats.
    if xml.tag == '{%s}ChannelHeartBeat' % schema:
      return
    debug(xmlstring)
    # Figuring out how to deal with events.
    if xml.tag == '{%s}Event' % schema:
      # Channel Term Events get handled differently.
      if xml.attrib['{%s}type' % w3] == 'xsi:ChannelTerminatedEvent':
        # Did we shut this channel down?  If not, bring up a replacement to
        # normalize our pool.  This improves stability of individual
        # channels (?).
        if not self.closing:
          self.make_conduit()
        if xml[0].text not in self.conduits:
          return
        return self.conduits.pop(xml[0].text).close()
      # Anything that's not a channel we want to record (for now at least).
      try:
        evttype = xml[6].attrib['{%s}type' % w3]
        if evttype == 'xsi:SubscriptionTerminatedEvent':
          # If this subscription was removed, but we're still running,
          # re-subscribe to it.
          if not self.closing:
            rxid = xml[4].text
            _c, _e, event = self.subscriptions.pop(rxid, [None] * 3)
            self.sequences.pop(rxid, None)
            if event:
              self.enterprise_subscribe(event)
      except:
        # Short XML or Not Sub Term Event.  Not a problem right now.
        self.processed.append(xml)
      # Next, find the event ID and respond to it.
      for item in xml.iter():
        if item.tag == '{%s}eventID' % schema:
          self.post(self.url(events, version, 'channel', 'eventresponse'),
                    self.xml_eventresponse(item.text).toxml("UTF-8"))
          break

  def Connection(self, host, port = 443):
    Klass = MyHTTP if port == 80 else MyHTTPS
    conn = Klass(host, port)
    return conn

  def xml_channel(self):
    # Build up the XML Content for the Event Conduit.
    XML = Document()
    channel = XML.createElement('Channel')
    channel.setAttribute('xmlns', schema)
    props = [('channelSetId', self.csid),
             ('priority', '1'),
             ('weight', '50'),
             ('expires', str(self.expconduits))]
    for name, val in props:
      elem = XML.createElement(name)
      elem.appendChild(XML.createTextNode(val))
      channel.appendChild(elem)
    XML.appendChild(channel)
    return XML

  def xml_usersub(self):
    XML = Document()
    sub = XML.createElement('Subscription')
    sub.setAttribute('xmlns', schema)
    props = [('targetIdType', 'User'),
             ('event', 'Advanced Call'),
             ('expires', '3600'),
             ('channelSetId', self.csid),
             ('applicationId', str(uuid1()))]
    for tag, text in props:
      elem = XML.createElement(tag)
      elem.appendChild(XML.createTextNode(text))
      sub.appendChild(elem)
    XML.appendChild(sub)
    return XML

  def xml_enterprisesub(self, event = 'Advanced Call'):
    XML = Document()
    sub = XML.createElement('Subscription')
    sub.setAttribute('xmlns', schema)
    props = [('event', event),
             ('expires', '1000'),
             ('channelSetId', self.csid),
             ('applicationId', str(uuid1()))]
    for tag, text in props:
      elem = XML.createElement(tag)
      elem.appendChild(XML.createTextNode(text))
      sub.appendChild(elem)
    XML.appendChild(sub)
    return XML

  def xml_eventresponse(self, eventid):
    XML = Document()
    sub = XML.createElement('EventResponse')
    sub.setAttribute('xmlns', schema)
    props = [('eventID', eventid),
             ('statusCode', '200'),
             ('reason', 'OK')]
    for tag, text in props:
      elem = XML.createElement(tag)
      elem.appendChild(XML.createTextNode(text))
      sub.appendChild(elem)
    XML.appendChild(sub)
    return XML

  def xml_rxrenew(self):
    XML = Document()
    sub = XML.createElement('Subscription')
    sub.setAttribute('xmlns', schema)
    exp = XML.createElement('expires')
    exp.appendChild(XML.createTextNode('3600'))
    sub.appendChild(exp)
    XML.appendChild(sub)
    return XML

  def xml_userdnd(self, state):
    XML = Document()
    dnd = XML.createElement('DoNotDisturb')
    dnd.setAttribute('xmlns', schema)
    props = [('active', 'true' if state else 'false'),
             ('ringSplash', 'false')]
    for tag, text in props:
      elem = XML.createElement(tag)
      elem.appendChild(XML.createTextNode(text))
      dnd.appendChild(elem)
    XML.appendChild(dnd)
    return XML

  def make_conduit(self):
    XML = self.xml_channel()
    body = XML.toxml()
    # Create the connection and POST to open the channel.
    R = self.post(self.url(async, events, version, 'channel'), body)
    if R.status == 200:
      respData = json.loads(R.read())
      cid = respData['Channel']['channelId']['$']
      self.conduits[cid] = R
      t = Thread(target = self.reader, name = 'Channel %s' % cid, args = (cid,))
      t.daemon = True
      t.start()
    else:
      debug(R.status, R.read())

  def post(self, url, body, connection = None):
    if connection is None:
      connection = self.Connection(baseurl)
    headers = EventsChannel.headers.copy()
    headers['Content-Length'] = '%d' % len(body)
    connection.request('POST', url, '', headers)
    if 'eventresponse' not in url:
      debug(body)
    connection.send(body)
    return connection.getresponse()

  def get(self, url):
    C = self.Connection(baseurl)
    C.request('GET', url, '', EventsChannel.headers)
    return C.getresponse()

  def put(self, url, data = ''):
    C = self.Connection(baseurl)
    C.request('PUT', url, data, EventsChannel.headers)
    return C.getresponse()

  def delete(self, url):
    C = self.Connection(baseurl)
    C.request('DELETE', url, '', EventsChannel.headers)
    return C.getresponse()

  def getchannelset(self):
    return self.get(self.url(events, version, 'channelset', self.csid))

  def getsubscriptionset(self):
    return [json.loads(self.getsubscription(rxid).read()) for rxid in self.subscriptions.keys()]

  def getsubscription(self, rxid):
    return self.get(self.url(events, version, 'subscription', rxid))

  def getuserprofile(self, userid):
    return self.get(self.url(actions, version, 'user', userid, 'profile'))

  def getuserservices(self, userid):
    return self.get(self.url(actions, version, 'user', userid, 'services'))

  def getusercalls(self, userid):
    return self.get(self.url(actions, version, 'user', userid, 'calls'))

  def getuserdnd(self, userid):
    return self.get(self.url(actions, version, 'user', userid, 'services', 'donotdisturb'))

  def getuserdevices(self, userid):
    return self.get(self.url(actions, version, 'user', userid, 'profile', 'device'))

  def setuserdnd(self, userid, state):
    XML = self.xml_userdnd(state)
    body = XML.toxml()
    R = self.put(self.url(actions, version, 'user', userid, 'services', 'DoNotDisturb'), body)
    if R.status != 200:
      debug(R.status, R.read())

  def usersubscribe(self, userid):
    # Build a the XML Request.
    XML = self.xml_usersub()
    body = XML.toxml()
    # Connect and post data.
    R = self.post(self.url(events, version, 'user', userid, 'subscription'), body)
    if R.status == 200:
      respData = json.loads(R.read())
      print respData

  def enterprise_subscribe(self, event = 'Advanced Call'):
    self.events.add(event)
    XML = self.xml_enterprisesub(event)
    body = XML.toxml()
    R = self.post(self.url(events, version, 'enterprise', enterprise), body)
    if R.status == 200:
      J = json.loads(R.read())
      RX = J['Subscription']
      rxid = RX['subscriptionId']['$']
      expires = int(RX['expires']['$'], 10)
      self.subscriptions[rxid] = (time.time(), expires, event)
      self.sequences[rxid] = 1
    elif R.status == 403:
      J = json.loads(R.read())
      if 'ErrorInfo' in J and J['ErrorInfo']['errorCode']['$'] == '110842':
        # Our Channel Set has gone missing?  Generate a new one.
        sleep(5)
        self.newchannelset()
        sleep(5)
        if not self.closing and not hasattr(sys, 'stopservice'):
          return self.newsubscriptions()
      else:
        debug(R.status, J)
    else:
      debug(R.status, R.read())

  def unsubscribe(self, rxid):
    return self.delete(self.url(events, version, 'subscription', rxid))

  def url(self, *args):
    # debug('/'.join(itertools.chain([''], args)))
    return '/'.join(itertools.chain([''], args))

if __name__ == '__main__':
  interact(local = globals())
