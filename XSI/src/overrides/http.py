#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''
Created on Mar 10, 2013

@author: daniel
'''

from httplib import HTTPResponse, HTTPConnection, HTTPSConnection, _MAXLINE
from time import sleep

class MyResponse(HTTPResponse):
  def _read_chunked(self, amt):
    chunk_left = self.chunk_left
    value = []
    while True:
      if self.fp is None:
        break
      if chunk_left is None:
        try:
          line = self.fp.readline(_MAXLINE + 1)
        except AttributeError:
          # self.fp becomes None when file closes.
          return ''.join(value)
        i = line.find(';')
        if i >= 0:
          line = line[:i] # strip chunk-extensions
        try:
          chunk_left = int(line, 16)
        except ValueError:
          # close the connection as protocol synchronization is
          # probably lost
          # self.close()
          sleep(2)
          continue
        if chunk_left == 0:
          break
      if amt is None:
        value.append(self._safe_read(chunk_left))
        return ''.join(value)
      elif amt < chunk_left:
        value.append(self._safe_read(amt))
        self.chunk_left = chunk_left - amt
        return ''.join(value)
      elif amt == chunk_left:
        value.append(self._safe_read(amt))
        self._safe_read(2)  # toss the CRLF at the end of the chunk
        self.chunk_left = None
        return ''.join(value)
      else:
        value.append(self._safe_read(chunk_left))
        amt -= chunk_left

      # we read the whole chunk, get another
      self._safe_read(2)      # toss the CRLF at the end of the chunk
      chunk_left = None

    # read and discard trailer up to the CRLF terminator
    ### note: we shouldn't have any trailers!
    while True and self.fp is not None:
      line = self.fp.readline(_MAXLINE + 1)
      if not line:
        # a vanishingly small number of sites EOF without
        # sending the trailer
        break
      if line == '\r\n':
        break

    # we read everything; close the "file"
    # self.close()
    return ''.join(value)


class MyHTTP(HTTPConnection):
  def __init__(self, *args, **kwargs):
    HTTPConnection.__init__(self, *args, **kwargs)
    self.response_class = MyResponse


class MyHTTPS(HTTPSConnection):
  def __init__(self, *args, **kwargs):
    HTTPSConnection.__init__(self, *args, **kwargs)
    self.response_class = MyResponse


