// static std::map<std::string, std::string> g_data;




// enum {
//   RES_OK = 0, // Ok response
//   RES_ERR = 1, // Error in response 
//   RES_NX = 2, // Response not found 
// };

// struct Response{
//   uint32_t status = 0;
//   std::vector<uint8_t> data;
// };




// static void make_response(const Response &resp, Buffer *out ){
//   uint32_t resp_len = 4 + (uint32_t) resp.data.size();
//   buf_append(out, (const uint8_t *)&resp_len, 4);
//   buf_append(out, (const uint8_t *)&resp.status, 4);
//   buf_append(out, resp.data.data(), resp.data.size());
// }








// static int32_t send_req(int fd, const uint8_t *text, size_t len){
//   if (len > k_max_msg) {return -1;}

//   std::vector<uint8_t> wbuf;
//   uint32_t len32 = (uint32_t)len;
//   buf_append(wbuf, (const uint8_t *)&len32, 4);// appends header 
//   buf_append(wbuf, text, len); // appends body
  
//   return write_all(fd,wbuf.data(),wbuf.size());
// }

// static int32_t read_res(int fd){
//   //we start with header body 
//   std::vector<uint8_t> rbuf;
//   rbuf.resize(4);
//   errno = 0;
//   int32_t err = read_full(fd, &rbuf[0], 4);
//   if (err) {
//     if (errno == 0){
//       msg("EOF - Client disconnected");
//     } else {
//       msg("read() error");
//     }
//     return err;
//   }
//   uint32_t len = 0;
//   memcpy(&len, rbuf.data(), 4);
//   if(len > k_max_msg){
//     msg("too long");
//     return -1;
//   }

//   // now the reply body
//   rbuf.resize(4 + len); // (header + body)
//   err = read_full(fd, &rbuf[4], len); 
//   if (err){
//     msg("read() error");
//     return err;
//   }
//   // the do something part
//   printf("len:%u data:%.*s\n", len, len < 100 ? len : 100, &rbuf[4]);
//   return 0; 

