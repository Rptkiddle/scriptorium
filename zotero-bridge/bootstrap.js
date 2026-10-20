/**
 * Scriptorium Bridge: local HTTP write endpoints for the scriptorium hub.
 *
 * Zotero's built-in local API (same port, /api/...) is read-only. This
 * plugin registers narrow, fixed-purpose write endpoints on Zotero's
 * localhost server (port 23119, 127.0.0.1 only) so hub writes land in the
 * local library instantly; Zotero's own sync uploads them in the
 * background. It is the hub's only write path.
 *
 * Endpoints (all JSON over POST, /scriptorium/...):
 *   ping                 -> {bridge, zotero}
 *   items/create         {items: [web-API item JSON], collectionKey?}
 *                        -> {created: [key], citation_keys: {key: ck}, failed}
 *   items/update         {key, patch} -> {updated, version}
 *                        (list fields in the patch replace the item's lists)
 *   items/tags           {keys, add?, remove?} -> {updated: {key: [tags]}, failed}
 *   items/collections    {keys, add?, remove?} -> {updated: {key: [collection keys]}, failed}
 *                        (add or remove single entries; the rest of each list stays)
 *   notes/create         {parentKey, note} -> {created, parentKey}
 *   items/delete         {key} -> {deleted}   (moves to trash)
 *   attachments/link     {parentKey, path, title?} -> {created: [key]}
 *   collections/create   {name, parentKey?} -> {created: [key]}
 *   collections/update   {key, name} -> {updated, name}   (rename)
 *   collections/delete   {key} -> {deleted}   (moves to trash; items stay)
 *   collections/move     {key, parentKey} -> {moved, parentKey}
 *                        (an empty parentKey moves it to the top level)
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

function byKey(key) {
  var item = Zotero.Items.getByLibraryAndKey(Zotero.Libraries.userLibraryID, key);
  if (!item) throw new Error("no item with key " + key);
  return item;
}

function collectionByKey(key) {
  var collection = Zotero.Collections.getByLibraryAndKey(Zotero.Libraries.userLibraryID, key);
  if (!collection || collection.deleted) throw new Error("no collection with key " + key);
  return collection;
}

function registerEndpoints(version) {
  endpoint("/scriptorium/ping", async function () {
    return { bridge: version, zotero: Zotero.version };
  });

  endpoint("/scriptorium/items/create", async function (data) {
    var created = [];
    var citationKeys = {};
    var failed = {};
    var items = data.items || [];
    for (var i = 0; i < items.length; i++) {
      try {
        var json = Object.assign({}, items[i]);
        if (data.collectionKey) {
          json.collections = (json.collections || []).concat([data.collectionKey]);
        }
        var item = new Zotero.Item(json.itemType);
        item.libraryID = Zotero.Libraries.userLibraryID;
        item.fromJSON(json);
        await item.saveTx();
        created.push(item.key);
        var ck = item.getField("citationKey");
        if (ck) citationKeys[item.key] = ck;
      } catch (e) {
        Zotero.logError(e);
        failed[String(i)] = String(e);
      }
    }
    return { created: created, citation_keys: citationKeys, failed: failed };
  });

  endpoint("/scriptorium/items/update", async function (data) {
    var item = byKey(data.key);
    var json = item.toJSON();
    Object.assign(json, data.patch || {});
    item.fromJSON(json);
    await item.saveTx();
    return { updated: item.key, version: item.version };
  });

  endpoint("/scriptorium/items/tags", async function (data) {
    var add = data.add || [];
    var remove = data.remove || [];
    var updated = {};
    var failed = {};
    var keys = data.keys || [];
    for (var i = 0; i < keys.length; i++) {
      try {
        var item = byKey(keys[i]);
        add.forEach(function (tag) { item.addTag(tag); });
        remove.forEach(function (tag) { item.removeTag(tag); });
        if (item.hasChanged()) await item.saveTx();
        updated[item.key] = item.getTags().map(function (t) { return t.tag; });
      } catch (e) {
        Zotero.logError(e);
        failed[keys[i]] = String(e);
      }
    }
    return { updated: updated, failed: failed };
  });

  endpoint("/scriptorium/items/collections", async function (data) {
    // Resolve every collection first, so an unknown key changes nothing.
    var addIDs = (data.add || []).map(function (k) { return collectionByKey(k).id; });
    var removeIDs = (data.remove || []).map(function (k) { return collectionByKey(k).id; });
    var updated = {};
    var failed = {};
    var keys = data.keys || [];
    for (var i = 0; i < keys.length; i++) {
      try {
        var item = byKey(keys[i]);
        addIDs.forEach(function (id) { item.addToCollection(id); });
        removeIDs.forEach(function (id) { item.removeFromCollection(id); });
        if (item.hasChanged()) await item.saveTx();
        updated[item.key] = item.getCollections().map(function (id) {
          return Zotero.Collections.get(id).key;
        });
      } catch (e) {
        Zotero.logError(e);
        failed[keys[i]] = String(e);
      }
    }
    return { updated: updated, failed: failed };
  });

  endpoint("/scriptorium/notes/create", async function (data) {
    var parent = byKey(data.parentKey);
    if (!parent.isRegularItem()) {
      throw new Error(data.parentKey + " is a " + parent.itemType +
        "; notes are added to regular items");
    }
    var note = new Zotero.Item("note");
    note.libraryID = parent.libraryID;
    note.parentID = parent.id;
    note.setNote(data.note || "");
    await note.saveTx();
    return { created: note.key, parentKey: parent.key };
  });

  endpoint("/scriptorium/items/delete", async function (data) {
    var item = byKey(data.key);
    item.deleted = true;
    await item.saveTx();
    return { deleted: data.key };
  });

  endpoint("/scriptorium/attachments/link", async function (data) {
    var parent = byKey(data.parentKey);
    var file = Zotero.File.pathToFile(data.path);
    if (!file.exists()) throw new Error("no file at " + data.path);
    var att = await Zotero.Attachments.linkFromFile({
      file: file,
      parentItemID: parent.id,
      title: data.title || undefined,
    });
    return { created: [att.key], file: data.path };
  });

  endpoint("/scriptorium/collections/create", async function (data) {
    var collection = new Zotero.Collection();
    collection.libraryID = Zotero.Libraries.userLibraryID;
    collection.name = data.name;
    if (data.parentKey) {
      var parent = Zotero.Collections.getByLibraryAndKey(
        Zotero.Libraries.userLibraryID,
        data.parentKey
      );
      if (!parent) throw new Error("no collection with key " + data.parentKey);
      collection.parentID = parent.id;
    }
    await collection.saveTx();
    return { created: [collection.key] };
  });

  endpoint("/scriptorium/collections/update", async function (data) {
    var collection = Zotero.Collections.getByLibraryAndKey(
      Zotero.Libraries.userLibraryID,
      data.key
    );
    if (!collection) throw new Error("no collection with key " + data.key);
    collection.name = data.name;
    await collection.saveTx();
    return { updated: collection.key, name: collection.name };
  });

  endpoint("/scriptorium/collections/move", async function (data) {
    var collection = Zotero.Collections.getByLibraryAndKey(
      Zotero.Libraries.userLibraryID,
      data.key
    );
    if (!collection) throw new Error("no collection with key " + data.key);
    if (data.parentKey) {
      var parent = Zotero.Collections.getByLibraryAndKey(
        Zotero.Libraries.userLibraryID,
        data.parentKey
      );
      if (!parent) throw new Error("no collection with key " + data.parentKey);
      collection.parentKey = parent.key;
    } else {
      collection.parentKey = false;
    }
    await collection.saveTx();
    return { moved: collection.key, parentKey: collection.parentKey || "" };
  });

  endpoint("/scriptorium/collections/delete", async function (data) {
    var collection = Zotero.Collections.getByLibraryAndKey(
      Zotero.Libraries.userLibraryID,
      data.key
    );
    if (!collection) throw new Error("no collection with key " + data.key);
    collection.deleted = true;
    await collection.saveTx();
    return { deleted: data.key };
  });

}

/* -- plugin lifecycle -------------------------------------------------- */

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
