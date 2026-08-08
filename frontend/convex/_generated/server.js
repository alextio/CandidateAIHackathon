/* eslint-disable */
/**
 * Generated utilities for implementing server-side Convex query and mutation functions.
 *
 * THIS CODE IS AUTOMATICALLY GENERATED.
 *
 * To regenerate, run `npx convex dev`.
 * @module
 */

import {
  actionGeneric,
  httpActionGeneric,
  queryGeneric,
  mutationGeneric,
  internalActionGeneric,
  internalMutationGeneric,
  internalQueryGeneric,
} from "convex/server";

/**
 * Define a query in this Convex app's public API.
 *
 * This function will be allowed to read your Convex database and will be accessible from the client.
 */
export const query = queryGeneric;

/**
 * Define a query that is only accessible from other Convex functions (but not from the client).
 */
export const internalQuery = internalQueryGeneric;

/**
 * Define a mutation in this Convex app's public API.
 *
 * This function will be allowed to modify your Convex database and will be accessible from the client.
 */
export const mutation = mutationGeneric;

/**
 * Define a mutation that is only accessible from other Convex functions (but not from the client).
 */
export const internalMutation = internalMutationGeneric;

/**
 * Define an action in this Convex app's public API.
 *
 * An action is a function which can execute any JavaScript code, including non-deterministic
 * code and code with side-effects, like calling third-party services.
 */
export const action = actionGeneric;

/**
 * Define an action that is only accessible from other Convex functions (but not from the client).
 */
export const internalAction = internalActionGeneric;

/**
 * Define an HTTP action.
 */
export const httpAction = httpActionGeneric;
