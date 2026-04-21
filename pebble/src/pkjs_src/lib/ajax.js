// ajax.js by meiguro - mit license

var ajax = (function(){

var formify = function(data) {
  var params = [], i = 0;
  for (var name in data) {
    params[i++] = encodeURIComponent(name) + '=' + encodeURIComponent(data[name]);
  }
  return params.join('&');
};

var deformify = function(form) {
  var params = {};
  form.replace(/(?:([^=&]*)=?([^&]*)?)(?:&|$)/g, function(_, name, value) {
    if (name) {
      params[name] = value || true;
    }
    return _;
  });
  return params;
};

// ajax options

// ajax allows you to make various http or https requests
var ajax = function(opt, success, failure) {
  if (typeof opt === 'string') {
    opt = { url: opt };
  }
  var method = opt.method || 'GET';
  var url = opt.url;
  //console.log(method + ' ' + url);

  var onHandler = ajax.onHandler;
  if (onHandler) {
    if (success) { success = onHandler('success', success); }
    if (failure) { failure = onHandler('failure', failure); }
  }

  if (opt.cache === false) {
    var appendSymbol = url.indexOf('?') === -1 ? '?' : '&';
    url += appendSymbol + '_=' + Date.now();
  }

  var req = new XMLHttpRequest();
  req.open(method.toUpperCase(), url, opt.async !== false);

  var headers = opt.headers;
  if (headers) {
    for (var name in headers) {
      req.setRequestHeader(name, headers[name]);
    }
  }

  var data = opt.data;
  if (data) {
    if (opt.type === 'json') {
      req.setRequestHeader('Content-Type', 'application/json');
      data = JSON.stringify(opt.data);
    } else if (opt.type === 'xml') {
      req.setRequestHeader('Content-Type', 'text/xml');
    } else if (opt.type !== 'text') {
      req.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
      data = formify(opt.data);
    }
  }

  var ready = false;
  req.onreadystatechange = function(e) {
    if (req.readyState === 4 && !ready) {
      ready = true;
      var body = req.responseText;
      var okay = req.status >= 200 && req.status < 300 || req.status === 304;

      try {
        if (opt.type === 'json') {
          body = JSON.parse(body);
        } else if (opt.type === 'form') {
          body = deformify(body);
        }
      } catch (err) {
        okay = false;
      }
      var callback = okay ? success : failure;
      if (callback) {
        callback(body, req.status, req);
      }
    }
  };

  req.send(data);
};

ajax.formify = formify;
ajax.deformify = deformify;

if (typeof module !== 'undefined') {
  module.exports = ajax;
} else {
  window.ajax = ajax;
}

return ajax;

})();
