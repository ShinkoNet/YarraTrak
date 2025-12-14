// this file provides an easy way to switch the actual implementation used by all the ui objects

var simply = {};

// Override this with the actual implementation you want to use.
simply.impl = undefined;

module.exports = simply;
