import { endpoint, jsonBody } from "../../lib/http.js";
import { oauthDevice } from "../../lib/service.js";

export default endpoint({ methods: ["POST"], oauth: true }, async (request) => {
  await jsonBody(request);
  return oauthDevice();
});
