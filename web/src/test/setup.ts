import { configure } from "@testing-library/dom";

/**
 * The suite renders whole screens (routing, redirects, lazy tabs) in parallel jsdom workers; on a
 * loaded machine the first render can take longer than the 1 s default of findBy/waitFor. A longer
 * ceiling changes no assertion, only how long a correct screen may take to appear.
 */
configure({ asyncUtilTimeout: 4000 });
