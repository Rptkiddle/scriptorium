/**
 * scriptorium-bridge: lets the Scriptorium client on this machine exchange
 * with this Zotero library. It adds items (with their PDFs) and reads which
 * publications and PDFs the library holds. It never changes or removes
 * anything that is in the library.
 *
 * The functions are endpoints on Zotero's own local server (port 23119,
 * reachable from this machine only), all JSON over POST under /scriptorium/:
 *
 *   ping                 -> {bridge, zotero}
 *   items/identifiers    -> {identifiers: ["doi:10.1/x", "arxiv:2501.01234"]}
 *                        the DOIs and arXiv IDs of the library's items, and
 *                        nothing else about them
 *   items/create         {items: [item JSON], collection?: name}
 *                        -> {created: {index: key}, failed: {index: reason}}
 *                        new items, placed in the named top-level collection
 *                        (made when missing)
 *   attachments/import   {parentKey, path} -> {created: key}
 *                        the PDF at path as a stored attachment of an item
 *   attachments/pdfs     {collections?: [name]}
 *                        -> {pdfs: [{path, identifiers}], without_identifier,
 *                            missing_file}
 *                        the library's PDFs (of the named collections and
 *                        their subcollections, or of all) whose item has a
 *                        DOI or arXiv ID
 */

var registeredPaths = [];

function endpoint(path, handler) {
  var Endpoints = Zotero.Server.Endpoints;
  Endpoints[path] = function () {};
  Endpoints[path].prototype = {
    supportedMethods: ["POST"],
    supportedDataTypes: ["application/json"],
    permitBookmarklet: false,
    init: async function (requestData) {
      try {
        var result = await handler(requestData.data || {});
        return [200, "application/json", JSON.stringify(result)];
      } catch (e) {
        Zotero.logError(e);
        return [500, "application/json", JSON.stringify({ error: String(e) })];
      }
    },
  };
  registeredPaths.push(path);
}

function library() {
  return Zotero.Libraries.userLibraryID;
}

/* -- identifiers ---------------------------------------------------------- */

var DOI = /10\.\d{4,9}\/[^\s"<>]+/;
var ARXIV_DOI = /^10\.48550\/arxiv\.(.+)$/i;
var ARXIV_ID = /(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?\/\d{7})(?:v\d+)?/i;

function field(item, name) {
  try {
    return String(item.getField(name) || "");
  } catch (e) {
    return ""; // the item's type has no such field
  }
}

function arxivID(text) {
  var m = ARXIV_ID.exec(text || "");
  return m ? m[1].toLowerCase() : "";
}

/** The DOIs and arXiv IDs an item carries: "doi:..." and "arxiv:..." in
 * lower case, from its DOI, archive ID, URL and the DOI and arXiv lines of
 * its Extra field. */
function identifiersOf(item) {
  var found = {};
  function doi(text) {
    var m = DOI.exec(text || "");
    if (!m) return;
    var value = m[0].toLowerCase().replace(/[.,;]+$/, "");
    var arxiv = ARXIV_DOI.exec(value);
    if (arxiv) {
      if (arxivID(arxiv[1])) found["arxiv:" + arxivID(arxiv[1])] = true;
    } else {
      found["doi:" + value] = true;
    }
  }
  function arxiv(text) {
    if (arxivID(text)) found["arxiv:" + arxivID(text)] = true;
  }
  doi(field(item, "DOI"));
  if (/arxiv/i.test(field(item, "archiveID"))) arxiv(field(item, "archiveID"));
  var url = field(item, "url");
  if (/arxiv\.org\/(abs|pdf)\//i.test(url)) arxiv(url.replace(/^.*arxiv\.org\/(abs|pdf)\//i, ""));
  else if (/doi\.org\//i.test(url)) doi(url);
  field(item, "extra").split("\n").forEach(function (line) {
    if (/^\s*doi\s*:/i.test(line)) doi(line);
    if (/^\s*arxiv\s*:/i.test(line)) arxiv(line.replace(/^\s*arxiv\s*:/i, ""));
  });
  return Object.keys(found);
}

/* -- collections ---------------------------------------------------------- */

function topLevelCollection(name) {
  var all = Zotero.Collections.getByLibrary(library(), false);
  for (var i = 0; i < all.length; i++) {
    if (!all[i].deleted && all[i].name === name) return all[i];
  }
  return null;
}

/** IDs of the collections with one of the names and of all collections
 * below them; an Error naming the names no collection has. */
function collectionIDs(names) {
  var all = Zotero.Collections.getByLibrary(library(), true);
  var ids = {};
  var missing = [];
  names.forEach(function (name) {
    var wanted = String(name).trim().toLowerCase();
    var hits = all.filter(function (c) {
      return !c.deleted && c.name.trim().toLowerCase() === wanted;
    });
    if (!hits.length) missing.push(name);
    hits.forEach(function (c) {
      ids[c.id] = true;
      c.getDescendents(false, "collection").forEach(function (d) {
        ids[d.id] = true;
      });
    });
  });
  if (missing.length) throw new Error("no collection named " + missing.join(", "));
  return ids;
}

/* -- endpoints ------------------------------------------------------------ */

function registerEndpoints(version) {
  endpoint("/scriptorium/ping", async function () {
    return { bridge: version, zotero: Zotero.version };
  });

  endpoint("/scriptorium/items/identifiers", async function () {
    var items = await Zotero.Items.getAll(library(), true, false);
    var found = {};
    items.forEach(function (item) {
      if (!item.isRegularItem()) return;
      identifiersOf(item).forEach(function (id) {
        found[id] = true;
      });
    });
    return { identifiers: Object.keys(found) };
  });

  endpoint("/scriptorium/items/create", async function (data) {
    var created = {};
    var failed = {};
    var items = data.items || [];
    var collection = null;
    if (data.collection && items.length) {
      collection = topLevelCollection(data.collection);
      if (!collection) {
        collection = new Zotero.Collection();
        collection.libraryID = library();
        collection.name = data.collection;
        await collection.saveTx();
      }
    }
    for (var i = 0; i < items.length; i++) {
      try {
        var json = Object.assign({}, items[i]);
        json.collections = collection ? [collection.key] : [];
        var item = new Zotero.Item(json.itemType);
        item.libraryID = library();
        item.fromJSON(json);
        await item.saveTx();
        created[String(i)] = item.key;
      } catch (e) {
        Zotero.logError(e);
        failed[String(i)] = String(e);
      }
    }
    return { created: created, failed: failed };
  });

  endpoint("/scriptorium/attachments/import", async function (data) {
    var parent = Zotero.Items.getByLibraryAndKey(library(), data.parentKey);
    if (!parent || !parent.isRegularItem()) throw new Error("no item with key " + data.parentKey);
    var title = Zotero.getString("attachment.fullText");
    var att = await Zotero.Attachments.importFromFile({
      file: data.path,
      parentItemID: parent.id,
      title: title,
      fileBaseName: Zotero.Attachments.getFileBaseNameFromItem(parent, { attachmentTitle: title }),
      contentType: "application/pdf",
    });
    return { created: att.key };
  });

  endpoint("/scriptorium/attachments/pdfs", async function (data) {
    var names = data.collections || [];
    var wanted = names.length ? collectionIDs(names) : null;
    var items = await Zotero.Items.getAll(library(), true, false);
    var pdfs = [];
    var withoutIdentifier = 0;
    var missingFile = 0;
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (!item.isRegularItem()) continue;
      if (wanted && !item.getCollections().some(function (id) { return wanted[id]; })) continue;
      var attachmentIDs = item.getAttachments();
      if (!attachmentIDs.length) continue;
      var identifiers = identifiersOf(item);
      var attachments = await Zotero.Items.getAsync(attachmentIDs);
      for (var j = 0; j < attachments.length; j++) {
        var att = attachments[j];
        if (!att.isFileAttachment() || att.attachmentContentType !== "application/pdf") continue;
        if (!identifiers.length) {
          withoutIdentifier++;
          continue;
        }
        var path = await att.getFilePathAsync();
        if (!path) {
          missingFile++;
          continue;
        }
        pdfs.push({ path: path, identifiers: identifiers });
      }
    }
    return { pdfs: pdfs, without_identifier: withoutIdentifier, missing_file: missingFile };
  });
}

/* -- plugin lifecycle ------------------------------------------------------ */

function install() {}

async function startup({ version }) {
  await Zotero.initializationPromise;
  registerEndpoints(version);
}

function shutdown() {
  for (var i = 0; i < registeredPaths.length; i++) {
    delete Zotero.Server.Endpoints[registeredPaths[i]];
  }
  registeredPaths = [];
}

function uninstall() {}
